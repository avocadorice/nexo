"""Temporary real SFTP receiver and Go worker used by benchmark.py --deliver."""

import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import paramiko


def stop(process):
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class DeliveryHarness:
    def __init__(self, binary, env, log, root):
        self.binary, self.env, self.log, self.root = binary, env, log, root
        self.worker = None

    def measure(self, conn, expected_rows, expected_files):
        started = time.perf_counter()
        self.worker = subprocess.Popen(
            [str(self.binary)], env=self.env, stdout=self.log, stderr=self.log
        )
        deadline, last_update = time.monotonic() + 180, 0
        while True:
            states = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state,count(*) AS n FROM batches GROUP BY state")
            }
            if states == {"acknowledged": expected_files}:
                break
            if states.get("rejected", 0) or states.get("conflict", 0):
                break
            if self.worker.poll() is not None:
                raise RuntimeError("benchmark worker exited before all acknowledgements")
            if time.monotonic() >= deadline:
                break
            if time.monotonic() - last_update >= 10:
                print(
                    f"Delivery: {states.get('acknowledged', 0)}/{expected_files} "
                    "files acknowledged",
                    file=sys.stderr,
                )
                last_update = time.monotonic()
            time.sleep(0.2)
        elapsed = time.perf_counter() - started
        with sqlite3.connect(self.root / "receipts.sqlite") as receiver:
            members, unique_members, files = receiver.execute(
                "SELECT count(*),count(DISTINCT chargeback_id),"
                "count(DISTINCT batch_id) FROM members"
            ).fetchone()
            receipt_rows, accepted_files = receiver.execute(
                "SELECT COALESCE(sum(row_count),0),count(*) FROM receipts WHERE status='accepted'"
            ).fetchone()
        attempts = {
            row["outcome"]: row["n"]
            for row in conn.execute(
                "SELECT outcome,count(*) AS n FROM delivery_attempts GROUP BY outcome"
            )
        }
        return {
            "elapsed_seconds": elapsed,
            "rows_per_second": expected_rows / elapsed,
            "batch_states": states,
            "attempt_outcomes": attempts,
            "receiver_members": members,
            "receiver_distinct_members": unique_members,
            "receiver_files": files,
            "receiver_accepted_receipt_rows": receipt_rows,
            "receiver_accepted_receipts": accepted_files,
            "worker_processes": 1,
            "worker_poll_seconds": 1,
            "acknowledgement_recheck_seconds": 30,
            "passed": states == {"acknowledged": expected_files}
            and members == unique_members == receipt_rows == expected_rows
            and files == accepted_files == expected_files,
        }


@contextmanager
def prepare_delivery(repo, settings):
    binary = repo / ".local/benchmark-worker"
    env = os.environ.copy()
    env.setdefault("GOMODCACHE", "/tmp/nexo-go-mod")
    env.setdefault("GOCACHE", "/tmp/nexo-go-cache")
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/worker"], cwd=repo, env=env, check=True
    )
    with tempfile.TemporaryDirectory(prefix="nexo-benchmark-") as directory:
        root = Path(directory)
        key = paramiko.RSAKey.generate(2048)
        key_path = root / "host_key"
        key.write_private_key_file(str(key_path))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        trust_path = root / "known_hosts"
        trust_path.write_text(f"[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n")
        env.update(
            {
                "SIM_ROOT": str(root),
                "SIM_HOST": "127.0.0.1",
                "SIM_PORT": str(port),
                "SIM_HOST_KEY": str(key_path),
                "SIM_MODE": "normal",
                "SFTP_ADDR": f"127.0.0.1:{port}",
                "SFTP_USER": "benchmark",
                "SFTP_PASSWORD": secrets.token_urlsafe(32),
                "SFTP_KNOWN_HOSTS": str(trust_path),
                "DATABASE_URL": settings.database_url,
                "S3_BUCKET": settings.bucket,
                "S3_ENDPOINT_URL": settings.endpoint,
                "AWS_REGION": settings.region,
                "WORKER_POLL_SECONDS": "1",
                "PYTHONPATH": str(repo / "src"),
            }
        )
        with (
            (root / "simulator.log").open("wb") as sim_log,
            (root / "worker.log").open("wb") as log,
        ):
            simulator = subprocess.Popen(
                [sys.executable, "-m", "nexo.cli", "simulator"],
                cwd=repo,
                env=env,
                stdout=sim_log,
                stderr=sim_log,
            )
            harness = DeliveryHarness(binary, env, log, root)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        if simulator.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("benchmark SFTP receiver did not start") from None
                        time.sleep(0.05)
                yield harness
            finally:
                stop(harness.worker)
                stop(simulator)
