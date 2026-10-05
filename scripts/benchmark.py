"""Measure real HTTP intake and PostgreSQL/S3 batching using isolated synthetic fixtures."""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import platform
import resource
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from nexo.batching import HEADER, csv_bytes, schedule, slot_cutoff  # noqa: E402
from nexo.config import Settings  # noqa: E402
from nexo.storage import s3_client  # noqa: E402


def percentile(samples, fraction):
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def provision_http(conn, count):
    customer = uuid4()
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO customers VALUES (%s,'Synthetic HTTP benchmark')", (customer,))
    conn.execute(
        "INSERT INTO tokens(hash,customer_id,role,expires_at) "
        "VALUES (%s,%s,'customer',clock_timestamp()+interval '1 hour')",
        (hashlib.sha256(token.encode()).hexdigest(), customer),
    )
    rows = conn.execute(
        "INSERT INTO transactions(id,customer_id,network,currency,amount_minor) "
        "SELECT gen_random_uuid(),%s,'visa','USD',12345 FROM generate_series(1,%s) "
        "RETURNING id",
        (customer, count),
    ).fetchall()
    return customer, token, [str(row["id"]) for row in rows]


async def measure_http(base_url, token, transactions, concurrency):
    jobs = iter(transactions)
    statuses, latencies, identities, errors = Counter(), [], set(), Counter()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20,
        limits=limits,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
    ) as client:

        async def sender():
            for transaction in jobs:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        "/api/chargebacks",
                        headers={"Idempotency-Key": transaction},
                        json={
                            "transaction_id": transaction,
                            "amount_minor": "12345",
                            "reason": "OTHER",
                        },
                    )
                    latencies.append((time.perf_counter() - started) * 1000)
                    statuses[str(response.status_code)] += 1
                    if response.status_code == 201:
                        body = response.json()
                        if body["transaction_id"] != transaction or body["amount_minor"] != "12345":
                            errors["response_identity_or_amount_mismatch"] += 1
                        identities.add(body["id"])
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    errors[type(error).__name__] += 1

        started = time.perf_counter()
        await asyncio.gather(*(sender() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
    return {
        "requests": len(transactions),
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "accepted_per_second": statuses["201"] / elapsed,
        "latency_p50_ms": percentile(latencies, 0.50) if latencies else None,
        "latency_p95_ms": percentile(latencies, 0.95) if latencies else None,
        "latency_p99_ms": percentile(latencies, 0.99) if latencies else None,
        "statuses": dict(statuses),
        "errors": dict(errors),
        "distinct_response_ids": len(identities),
        "retry_policy": "none; every attempted HTTP request is included",
    }


def provision_batches(conn, count, cutoff):
    customer = uuid4()
    conn.execute("INSERT INTO customers VALUES (%s,'Synthetic batch benchmark')", (customer,))
    # SQL generates fixture identifiers directly, avoiding a 100,000-row Python staging list.
    conn.execute(
        "CREATE TEMP TABLE benchmark_fixture AS SELECT gen_random_uuid() AS transaction_id,"
        "gen_random_uuid() AS chargeback_id,mod(n,4)::int AS partition,"
        "CASE WHEN mod(n/4,2)=0 THEN 'visa' ELSE 'mastercard' END AS network "
        "FROM generate_series(1,%s) AS n",
        (count,),
    )
    conn.execute(
        "INSERT INTO transactions(id,customer_id,network,currency,amount_minor) "
        "SELECT transaction_id,%s,network,'USD',12345 FROM benchmark_fixture",
        (customer,),
    )
    conn.execute(
        "INSERT INTO chargebacks(id,customer_id,transaction_id,idempotency_key,request_hash,"
        "amount_minor,reason,received_at,partition) "
        "SELECT chargeback_id,%s,transaction_id,chargeback_id::text,repeat('b',64),"
        "12345,'OTHER',%s,partition FROM benchmark_fixture",
        (customer, cutoff - timedelta(minutes=1)),
    )
    conn.execute("DROP TABLE benchmark_fixture")
    conn.execute("ANALYZE chargebacks")
    conn.execute("ANALYZE transactions")
    return customer


def verify_files(conn, client, bucket, customer, expected):
    manifests = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    seen, duplicates, digest_failures, row_failures = set(), 0, 0, 0
    largest_file, total_bytes = 0, 0
    for batch in manifests:
        response = client.get_object(Bucket=bucket, Key=batch["object_key"])
        with response["Body"] as body:
            data = body.read()
        total_bytes += len(data)
        largest_file = max(largest_file, len(data))
        if hashlib.sha256(data).hexdigest() != batch["sha256"] or len(data) != batch["byte_count"]:
            digest_failures += 1
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
        if reader.fieldnames != HEADER:
            row_failures += 1
        rows = list(reader)
        recorded = conn.execute(
            "SELECT c.id AS chargeback_id,c.transaction_id,t.network,t.currency,"
            "c.amount_minor,c.reason FROM chargebacks c "
            "JOIN transactions t ON t.id=c.transaction_id WHERE c.batch_id=%s ORDER BY c.id",
            (batch["id"],),
        ).fetchall()
        canonical = [{key: str(row[key]) for key in HEADER} for row in recorded]
        if rows != canonical or len(rows) != batch["row_count"]:
            row_failures += 1
        for row in rows:
            if row["chargeback_id"] in seen:
                duplicates += 1
            seen.add(row["chargeback_id"])
    database = conn.execute(
        "SELECT count(*) AS accepted,count(batch_id) AS assigned,"
        "count(DISTINCT id) AS distinct_id FROM chargebacks WHERE customer_id=%s",
        (customer,),
    ).fetchone()
    return {
        "files": len(manifests),
        "csv_rows": len(seen),
        "duplicate_csv_memberships": duplicates,
        "digest_or_byte_count_failures": digest_failures,
        "csv_membership_or_financial_field_failures": row_failures,
        "all_rows_assigned_once": database["assigned"] == database["distinct_id"] == expected,
        "all_csv_members_present_once": len(seen) == expected and duplicates == 0,
        "all_files_archived": all(batch["state"] != "building" for batch in manifests),
        "largest_csv_bytes": largest_file,
        "total_csv_bytes": total_bytes,
    }


def runtime_details(conn):
    relevant = [
        "cmd/api/main.go",
        "internal/api/api.go",
        "internal/auth/auth.go",
        "migrations/001_initial.sql",
        "src/nexo/batching.py",
        "src/nexo/storage.py",
        "scripts/benchmark.py",
        "scripts/benchmark_delivery.py",
        "src/nexo/simulator.py",
        "internal/delivery/worker.go",
        "internal/delivery/clients.go",
    ]
    details = {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "go": subprocess.check_output(["go", "version"], text=True).strip(),
        "postgresql": conn.execute("SHOW server_version").fetchone()["server_version"],
        "libraries": {name: version(name) for name in ("httpx", "psycopg", "boto3")},
        "source_sha256": {
            name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in relevant
        },
        "topology": (
            "host Go API/Python client -> local PostgreSQL container; S3 port-forward to kind"
        ),
        "database_pool_max": 10,
        "api_in_flight_limit": 200,
        "tls": "disabled on these loopback/local test endpoints",
    }
    if sys.platform == "darwin":
        details["cpu_model"] = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
        details["host_memory_bytes"] = int(
            subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        )
    return details


def run(args):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise ValueError("TEST_DATABASE_URL must name an isolated test database")
    endpoint = os.environ.get("TEST_S3_ENDPOINT", "http://127.0.0.1:8333")
    credentials = Path(os.environ.get("TEST_S3_CREDENTIALS", REPO / ".local/local-secrets.json"))
    values = json.loads(credentials.read_text())
    os.environ["AWS_ACCESS_KEY_ID"] = values["s3_key"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = values["s3_secret"]
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    schema = "benchmark_" + uuid4().hex
    options = conninfo_to_dict(database_url)
    options["options"] = f"-csearch_path={schema}"
    isolated_url = make_conninfo(**options)
    bucket = "nexo-benchmark-" + uuid4().hex
    settings = Settings(isolated_url, bucket, endpoint, "us-east-1", 4, args.batch_rows, 200)
    client = s3_client(settings)
    process, bucket_created = None, False
    result = {"measured_at_utc": datetime.now(UTC).isoformat()}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with (
                ExitStack() as processes,
                psycopg.connect(isolated_url, autocommit=True, row_factory=dict_row) as conn,
            ):
                for migration in sorted((REPO / "migrations").glob("*.sql")):
                    conn.execute(migration.read_text())
                client.create_bucket(Bucket=bucket)
                bucket_created = True
                result["runtime"] = runtime_details(conn)
                cutoff = slot_cutoff(datetime.now(UTC))
                print(
                    f"Provisioning {args.requests:,} synthetic HTTP transactions", file=sys.stderr
                )
                customer, token, transactions = provision_http(conn, args.requests)
                build_env = os.environ.copy()
                build_env.setdefault("GOMODCACHE", "/tmp/nexo-go-mod")
                build_env.setdefault("GOCACHE", "/tmp/nexo-go-cache")
                binary = REPO / ".local/benchmark-api"
                binary.parent.mkdir(exist_ok=True)
                subprocess.run(
                    ["go", "build", "-o", str(binary), "./cmd/api"],
                    cwd=REPO,
                    env=build_env,
                    check=True,
                )
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                server_env = os.environ | {
                    "DATABASE_URL": isolated_url,
                    "API_ADDR": f"127.0.0.1:{port}",
                    "DB_POOL_MAX": "10",
                    "PARTITION_COUNT": "4",
                }
                with tempfile.TemporaryFile() as api_log:
                    process = subprocess.Popen(
                        [str(binary)], cwd=REPO, env=server_env, stdout=api_log, stderr=api_log
                    )
                    base_url = f"http://127.0.0.1:{port}"
                    deadline = time.monotonic() + 15
                    with httpx.Client(trust_env=False, timeout=1) as probe:
                        while True:
                            try:
                                if probe.get(base_url + "/readyz").status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError("benchmark API did not become ready")
                            time.sleep(0.05)
                    print(
                        f"Measuring HTTP intake at concurrency {args.concurrency}", file=sys.stderr
                    )
                    result["http"] = asyncio.run(
                        measure_http(base_url, token, transactions, args.concurrency)
                    )
                    process.terminate()
                    process.wait(timeout=15)
                    process = None
                counts = conn.execute(
                    "SELECT count(*) AS rows,count(DISTINCT transaction_id) AS transactions "
                    "FROM chargebacks WHERE customer_id=%s",
                    (customer,),
                ).fetchone()
                result["http"]["database_rows"] = counts["rows"]
                result["http"]["unique_transactions"] = counts["transactions"]
                result["http"]["accepted_audit_events"] = conn.execute(
                    "SELECT count(*) AS n FROM audit_events WHERE event='chargeback.accepted'"
                ).fetchone()["n"]
                print(f"Provisioning {args.rows:,} pre-cutoff batch fixtures", file=sys.stderr)
                started = time.perf_counter()
                batch_customer = provision_batches(conn, args.rows, cutoff)
                result["batch_seed_seconds"] = time.perf_counter() - started
                delivery = None
                if args.deliver:
                    from benchmark_delivery import prepare_delivery

                    delivery = processes.enter_context(prepare_delivery(REPO, settings))
                print("Measuring claims, CSV generation and real S3 writes", file=sys.stderr)
                tracemalloc.start()
                started, runs, claimed = time.perf_counter(), 0, 0
                while True:
                    progress = schedule(settings, cutoff)
                    if progress.get("busy"):
                        raise RuntimeError(
                            "another scheduler holds the test database advisory lock"
                        )
                    claimed += progress["claimed"]
                    runs += 1
                    if progress["complete"]:
                        break
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                max_row = {
                    "chargeback_id": "f" * 36,
                    "transaction_id": "f" * 36,
                    "network": "mastercard",
                    "currency": "USD",
                    "amount_minor": "9223372036854775807",
                    "reason": "NOT_RECEIVED",
                }
                header_bytes = len(csv_bytes([]))
                maximum_row_bytes = len(csv_bytes([max_row])) - header_bytes
                result["batching"] = {
                    "rows": args.rows,
                    "elapsed_seconds": elapsed,
                    "rows_per_second": args.rows / elapsed,
                    "schedule_runs": runs,
                    "claimed_files": claimed,
                    "max_rows_per_file": args.batch_rows,
                    "logical_partitions": 4,
                    "scheduler_workers": 1,
                    "peak_traced_python_allocation_bytes": peak,
                    "python_process_peak_rss_bytes": peak_rss
                    if sys.platform == "darwin"
                    else peak_rss * 1024,
                    "schema_maximum_csv_bytes_per_configured_file": header_bytes
                    + maximum_row_bytes * args.batch_rows,
                    "memory_measurement": (
                        "tracemalloc active throughout timed batching; adds overhead"
                    ),
                }
                if delivery:
                    print(
                        "Measuring real SFTP delivery and receiver acknowledgement", file=sys.stderr
                    )
                    result["delivery"] = delivery.measure(conn, args.rows, claimed)
                    result["drain_elapsed_seconds"] = time.perf_counter() - started
                print("Verifying every stored CSV against its database membership", file=sys.stderr)
                result["verification"] = verify_files(
                    conn, client, bucket, batch_customer, args.rows
                )
                result["targets"] = {
                    "http_100_accepted_per_second": result["http"]["accepted_per_second"] >= 100,
                    "batch_100000_within_six_hours": args.rows >= 100000 and elapsed < 21600,
                    "full_drain_100000_within_six_hours": bool(delivery)
                    and args.rows >= 100000
                    and result["delivery"]["passed"]
                    and result["drain_elapsed_seconds"] < 21600,
                    "cloud_capacity_validated": False,
                }
                result["passed"] = (
                    result["http"]["statuses"] == {"201": args.requests}
                    and not result["http"]["errors"]
                    and result["http"]["distinct_response_ids"] == args.requests
                    and counts["rows"] == counts["transactions"] == args.requests
                    and result["http"]["accepted_audit_events"] == args.requests
                    and result["verification"]["all_rows_assigned_once"]
                    and result["verification"]["all_csv_members_present_once"]
                    and result["verification"]["all_files_archived"]
                    and result["verification"]["digest_or_byte_count_failures"] == 0
                    and result["verification"]["csv_membership_or_financial_field_failures"] == 0
                    and (not delivery or result["delivery"]["passed"])
                )
                output.write_text(json.dumps(result, indent=2) + "\n")
                print(
                    f"HTTP: {result['http']['accepted_per_second']:.1f} accepted/s; "
                    f"p50 {result['http']['latency_p50_ms']:.1f} ms; "
                    f"p95 {result['http']['latency_p95_ms']:.1f} ms. "
                    f"Batching: {args.rows:,} rows in {elapsed:.2f} s. "
                    f"Integrity: {'PASS' if result['passed'] else 'FAIL'}. Output: {output}"
                )
                return result["passed"]
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            try:
                if bucket_created:
                    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                        if objects:
                            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                    client.delete_bucket(Bucket=bucket)
            finally:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--batch-rows", type=int, default=1000)
    parser.add_argument(
        "--deliver", action="store_true", help="include real SFTP and acknowledgements"
    )
    parser.add_argument("--output", default=str(REPO / ".local/benchmark-results.json"))
    args = parser.parse_args()
    if not 1 <= args.requests <= 100000 or not 1 <= args.rows <= 10000000:
        parser.error("requests must be 1..100000 and rows must be 1..10000000")
    if not 1 <= args.concurrency <= 200 or not 1 <= args.batch_rows <= 10000:
        parser.error("concurrency must be 1..200 and batch-rows must be 1..10000")
    sys.exit(0 if run(args) else 1)


if __name__ == "__main__":
    main()
