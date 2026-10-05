# Recovery and restore verification

Durable PostgreSQL state assigns each chargeback to one permanent batch. That guarantee depends on retaining the assignment and publication history. **A historical database restore must not automatically resume the delivery worker or scheduler:** a file may already exist remotely even though its batch record was written after the restored point. Reassigning those chargebacks could put them in a second submitted file.

## Local logical restore drill

Run after a synthetic end-to-end flow has produced batch membership, file metadata, delivery attempts, and audit events:

```sh
./scripts/restore-check.sh
```

The script explicitly targets the local `kind-nexo` cluster and the `nexo` database. It reads a consistent PostgreSQL custom-format dump, creates an isolated `nexo_restore_<timestamp>` database in the same local server, restores schema/data/indexes/triggers, and compares all table counts before/after. It verifies financial ownership/amount bounds, batch network/partition/cutoff membership, row counts, unique object keys, and attempt relationships. A concurrently changing source causes the count comparison to fail and requires a quiet rerun.

The temporary restored database is dropped after verification, including failure paths. The private dump, its SHA-256 digest, counts, timings, and result remain in ignored `.local/recovery/<timestamp>/`. Dumps contain application data and token hashes; retain them outside Git. This uses real `pg_dump` and `pg_restore` against real PostgreSQL, and does not modify the live database's rows.

The drill passed on **5 October 2026 at 14:37 PDT** against the running kind cluster. All nine table counts matched, including **9 chargebacks, 8 batches, 16 delivery attempts, and 81 audit events**. All six integrity checks returned zero violations. The 33,816-byte dump took 0.236 seconds; restore took 0.166 seconds; the full drill took 1.056 seconds. The isolated restored database was then removed. Local evidence is in `.local/recovery/20261005T213705Z/report.json`.

This drill establishes that a particular small local snapshot can be restored with its relational integrity. It does **not** restore CSV objects, SFTP remote files, or cloud infrastructure. Local elapsed time is not a cloud recovery-time promise, and copying a dump is not a zero-data-loss backup strategy.

## Historical cloud database recovery

This is an operations procedure to validate in the real account. It is not yet an automated or exercised recovery capability.

1. Stop customer writes, suspend the scheduling CronJob, and scale delivery workers to zero. Preserve the surviving database, object store, simulator/recipient files, and logs. Do not reset unknown states or release assigned rows.
2. Restore the managed PostgreSQL backup to a separate database cluster. Keep Nexo workloads disconnected until the restore is checked. Use verified TLS and repeat the relational/financial checks from the local drill.
3. Inventory every immutable CSV object and every recipient filename/acknowledgement after the backup point. Compare filenames, hashes, row counts, batch identities, and publication attempts to the restored database. A recipient timeout is an unknown outcome, not evidence that a file is absent.
4. Reconstruct missing publication evidence from verified artifacts and recipient records under an explicit reviewed repair. Preserve original batch/file identity. If evidence is incomplete, hold those chargebacks and expose the gap for operations; never automatically place them into a new file.
5. Verify representative customer statuses and retained audit history, then resume only reconciled work. Record the incident, restored point, outstanding uncertainty, and observed recovery duration.

Nexo currently has no automated cross-store disaster-recovery reconciler or proven cloud recovery point objective (RPO: maximum acceptable lost history) / recovery time objective (RTO: time to restore service). Managed backups reduce database operations work but do not solve the distributed publication problem. Routine process restarts and ambiguous uploads use the durable normal-path reconciliation; historical data loss needs the stronger procedure above.
