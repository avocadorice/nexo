# Measured local performance

On **2026-10-05 at 14:45 PDT (21:45 UTC)**, the real-service benchmark accepted all 1,000 HTTP requests and drained 100,000 already queued synthetic chargebacks through real S3, SFTP and matching receiver acknowledgements in 48.93 seconds, without missing or duplicate CSV membership. These are local measurements, not cloud capacity evidence.

| Measurement | Observed result |
| --- | ---: |
| HTTP requests / simultaneous senders | 1,000 / 16 |
| HTTP 201 responses / errors / retries | 1,000 / 0 / 0 |
| HTTP elapsed time | 1.974 seconds |
| Accepted requests per second | 506.7 |
| Request latency p50 / p95 / p99 | 16.4 / 112.7 / 194.5 ms |
| Distinct accepted rows / transactions / intake audit events | 1,000 / 1,000 / 1,000 |
| Batch fixture rows | 100,000 |
| Claim, generate and archive elapsed time | 12.287 seconds |
| Batch processing rate | 8,138 rows/second |
| Scheduler processes / schedule invocations | 1 / 1 |
| Logical partitions / networks | 4 / 2 |
| Files / configured row cap | 104 / 1,000 |
| SFTP delivery and acknowledgement elapsed time | 36.381 seconds |
| Whole drain, from first claim through final acknowledgement | 48.928 seconds |
| Receiver distinct members / accepted receipts / receipt rows | 100,000 / 104 / 100,000 |
| Final acknowledged / unknown / rejected / conflict batches | 104 / 0 / 0 / 0 |
| Verified CSV rows / duplicate memberships | 100,000 / 0 |
| Digest, byte count, membership or financial-field mismatches | 0 |
| Largest observed CSV / total CSV bytes | 102,067 / 9,906,968 bytes |
| Peak traced Python allocation during batching | 1,747,193 bytes (1.67 MiB) |
| Python process peak resident memory through batching | 85,770,240 bytes (81.80 MiB) |

The 104 files result from eight independent network/partition groups, each containing 12,500 rows: twelve full files and one 500-row file per group. Every stored object was downloaded through the real S3 client. Its SHA-256 and byte count matched the persisted manifest, and every CSV row matched the database membership, ordering, identifiers, currency, amount and reason. All batches reached `acknowledged`; every batch fixture row appeared in exactly one file and one receiver membership record. The durable delivery history contains 104 `submitted` attempts followed by 104 `acknowledged` attempts, with no uncertain or failed outcomes.

## What was measured

[scripts/benchmark.py](../scripts/benchmark.py) builds and starts the actual Go API on an ephemeral loopback port, with a generated customer token and a private random PostgreSQL schema. An asynchronous Python HTTP client sends distinct chargeback requests through normal authentication, validation, database transactions and audit triggers. The database pool has ten connections; API admission allows at most 200 in-flight requests. No failed requests are retried or hidden. Latency runs from starting each HTTP request to receiving its response and therefore includes waiting for the database pool.

The batch phase inserts a separate set of explicit pre-cutoff fixtures, then calls the actual scheduler, atomic claim logic and CSV builder with a real S3-compatible client. Fixture insertion took 6.990 seconds and is excluded from the 12.287-second batch measurement. The benchmark uses a private temporary S3 bucket. With `--deliver`, [scripts/benchmark_delivery.py](../scripts/benchmark_delivery.py) also prepares a separate real Python SFTP simulator with generated credentials and a pinned SSH host key, then starts the actual Go delivery worker with a one-second polling interval. The worker retains its normal 30-second acknowledgement recheck delay, which accounts for most of the observed 36.381-second delivery phase. The whole-drain clock starts before the first claim and stops after all 104 database acknowledgements are observed. Builds, fixture preparation and receiver startup are excluded. Every archived file is checked afterward; cleanup removes the private schema, bucket and receiver files and stops the owned processes. It never prints or stores the generated token or S3 credentials in the result report.

CSV generation materializes one capped batch at a time. Given the schema's maximum field lengths, a 1,000-row CSV is bounded by **123,067 bytes**, including the header and line endings; the hard 10,000-row limit gives 1,230,067 bytes. These are CSV byte bounds, not whole-process memory bounds. Python row objects, strings and SDK buffers also consume memory. `tracemalloc` was active throughout timed batching and reported its peak Python allocations; this instrumentation adds overhead. Resident memory is the Python process's high-water mark, including libraries and earlier fixture work, and excludes the API, PostgreSQL and object-storage processes.

## Environment and limits of the result

- Host: Apple M4, ten logical CPUs, 16 GiB memory, macOS 15.8.1, arm64. The Go API, Go delivery worker, Python load generator and separate Python receiver ran directly on the host without container resource limits.
- Runtime: Go 1.27.1, Python 3.12.14, HTTPX 0.28.1, Psycopg 3.3.6 and Boto3 1.43.108.
- PostgreSQL: `postgres:17.6-alpine`, a separate local test container on port 55432, with no explicit container CPU or memory limit.
- Container host: isolated `colima-nexo` virtual machine with four virtual CPUs and 6 GiB memory. It also ran the single-node kind cluster.
- Object storage: real SeaweedFS 4.48 in kind, reached through a Kubernetes port-forward. The pod's verified limit was one CPU and 512 MiB memory; requests were 100 millicores and 128 MiB.
- Local transport: loopback HTTP and unencrypted local database connections; SFTP still used real authenticated SSH encryption and host-key verification. Production HTTP/database TLS overhead, cloud network latency and managed-service limits are absent from this measurement.

The HTTP run exceeded the initial 100 accepted requests/second target for this **2.0-second sample**. It does not establish sustained throughput, maximum capacity, service-level latency, long-term database growth or headroom. The 100,000-row backlog drained through acknowledged SFTP delivery in under six hours in this measured local topology. Those 100,000 rows were seeded directly as already accepted fixtures; only the separate 1,000-request test measured HTTP intake. The separately executed delivery integration tests verify timeout, partial-upload and ambiguous-publication recovery; the capacity run used normal receiver behavior.

This was one run on a shared development machine while other project work was active. It is not a benchmark of the Kubernetes API deployment or any cloud instance size. Repeat sustained runs and end-to-end measurements on the approved cloud topology before using these results for sizing or availability claims.

## Reproduce

Run from the `nexo/` directory after `uv sync --locked`, with Go installed and the local kind deployment running. Use a dedicated test database; the script creates and drops only a uniquely named schema there. The test container used for this record can be created with:

```sh
docker --context colima-nexo run -d --name nexo-test-postgres \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=nexo_test \
  -p 127.0.0.1:55432:5432 postgres:17.6-alpine
```

Skip container creation if that test container already exists. Its password-free authentication is restricted to the loopback test port; it is not the application deployment's database configuration. In another terminal expose the existing local object-storage service:

```sh
KUBECONFIG="$PWD/.local/kubeconfig" kubectl --context kind-nexo \
  -n nexo port-forward service/object-storage 8333:8333
```

Run the default measurement:

```sh
TEST_DATABASE_URL='postgres://postgres@127.0.0.1:55432/nexo_test?sslmode=disable' \
TEST_S3_ENDPOINT='http://127.0.0.1:8333' \
TEST_S3_CREDENTIALS="$PWD/.local/local-secrets.json" \
  .venv/bin/python scripts/benchmark.py --deliver
```

The credential file is the ignored local deployment file; the script reads only its `s3_key` and `s3_secret` fields. Flags `--requests`, `--concurrency`, `--rows`, `--batch-rows` and `--output` support larger or smaller runs. Omit `--deliver` to measure only intake and archival. Defaults are 1,000 requests, concurrency 16, 100,000 batch rows and 1,000 rows per file.

The machine-readable report is written to ignored `.local/benchmark-results.json`. It contains runtime versions, measured counts and durations, and SHA-256 fingerprints of the relevant source files, making an individual run attributable even before a commit exists. This document records the measured run above; rerunning the script does not silently rewrite its conclusions. Stop the dedicated test container when finished with `docker --context colima-nexo stop nexo-test-postgres`; the script cleans its fixtures automatically, while leaving the reusable local services running.
