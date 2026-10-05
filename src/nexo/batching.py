"""Bounded, recoverable batch claims and immutable CSV generation."""

import csv
import hashlib
import io
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from nexo.storage import s3_client

log = logging.getLogger(__name__)
HEADER = ["chargeback_id", "transaction_id", "network", "currency", "amount_minor", "reason"]


def slot_cutoff(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("slot needs an explicit timezone")
    now = now.astimezone(UTC)
    return now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)


def claim_batch(conn, cutoff, network, partition, limit):
    with conn.transaction():
        # Locked rows belong to one claimant. Membership is committed with the new batch.
        rows = conn.execute(
            """
            SELECT c.id FROM chargebacks c JOIN transactions t ON t.id=c.transaction_id
            WHERE c.batch_id IS NULL AND c.received_at < %s AND t.network=%s
              AND c.partition=%s
            ORDER BY c.received_at, c.id LIMIT %s FOR UPDATE OF c SKIP LOCKED
            """,
            (cutoff, network, partition, limit),
        ).fetchall()
        if not rows:
            return None
        batch_id = uuid4()
        conn.execute(
            """INSERT INTO batches(id,slot,network,partition,state,row_count)
                        VALUES (%s,%s,%s,%s,'building',%s)""",
            (batch_id, cutoff, network, partition, len(rows)),
        )
        conn.execute(
            "UPDATE chargebacks SET batch_id=%s WHERE id=ANY(%s)",
            (batch_id, [row["id"] for row in rows]),
        )
        conn.execute(
            """INSERT INTO audit_events(batch_id,event,details)
                        VALUES (%s,'batch_claimed',%s::jsonb)""",
            (batch_id, json.dumps({"row_count": len(rows)})),
        )
        return batch_id


def csv_bytes(rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in rows:
        writer.writerow([str(row[field]) for field in HEADER])
    return out.getvalue().encode("utf-8")


def build_batch(conn, s3, bucket, batch_id):
    with conn.transaction():
        batch = conn.execute("SELECT * FROM batches WHERE id=%s FOR UPDATE", (batch_id,)).fetchone()
        if batch is None or batch["state"] != "building":
            return False
        rows = conn.execute(
            """
            SELECT c.id chargeback_id,c.transaction_id,t.network,t.currency,c.amount_minor,c.reason
            FROM chargebacks c JOIN transactions t ON t.id=c.transaction_id
            WHERE c.batch_id=%s ORDER BY c.id LIMIT 10001
            """,
            (batch_id,),
        ).fetchall()
        if not rows or len(rows) != batch["row_count"] or len(rows) > 10000:
            raise ValueError("batch membership does not match its bounded manifest")
        data = csv_bytes(rows)
        digest = hashlib.sha256(data).hexdigest()
        key = f"exports/{batch['network']}/{batch['slot']:%Y%m%dT%H%MZ}/{batch_id}.csv"
        # A crash after S3 stores the file can only regenerate the same key and exact bytes.
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType="text/csv",
            Metadata={"sha256": digest, "batch-id": str(batch_id)},
        )
        conn.execute(
            """UPDATE batches SET state='ready',object_key=%s,sha256=%s,
                        byte_count=%s,updated_at=clock_timestamp() WHERE id=%s""",
            (key, digest, len(data), batch_id),
        )
        conn.execute(
            """INSERT INTO audit_events(batch_id,event,details)
                        VALUES (%s,'file_ready',%s::jsonb)""",
            (batch_id, json.dumps({"sha256": digest, "bytes": len(data)})),
        )
        log.info("file_ready batch_id=%s rows=%d bytes=%d", batch_id, len(rows), len(data))
        return True


def schedule(settings, cutoff=None):
    cutoff = cutoff or slot_cutoff(datetime.now(UTC))
    if cutoff != slot_cutoff(cutoff) or cutoff > datetime.now(UTC):
        raise ValueError("cutoff must be a past or current six-hour UTC boundary")
    s3 = s3_client(settings)
    built = claimed = 0
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        # Cron may launch duplicate Jobs. This lock bounds concurrent scheduler work;
        # durable slots and row membership still survive a lost process or replay.
        locked = conn.execute("SELECT pg_try_advisory_lock(817203, 1) AS ok").fetchone()["ok"]
        if not locked:
            return {"busy": True, "claimed": 0, "built": 0}
        conn.execute("INSERT INTO slots(cutoff) VALUES (%s) ON CONFLICT DO NOTHING", (cutoff,))
        pending = conn.execute(
            "SELECT id FROM batches WHERE state='building' ORDER BY created_at LIMIT %s",
            (settings.schedule_max_batches,),
        ).fetchall()
        for row in pending:
            built += build_batch(conn, s3, settings.bucket, row["id"])
        slot = conn.execute("SELECT completed_at FROM slots WHERE cutoff=%s", (cutoff,)).fetchone()
        if slot["completed_at"] is not None:
            return {"claimed": 0, "built": built, "complete": True}
        partitions = [
            row["partition"]
            for row in conn.execute(
                "SELECT DISTINCT partition FROM chargebacks "
                "WHERE batch_id IS NULL AND received_at<%s",
                (cutoff,),
            ).fetchall()
        ]
        for _ in range(settings.schedule_max_batches):
            found = False
            for network in ("visa", "mastercard"):
                for partition in partitions:
                    if claimed >= settings.schedule_max_batches:
                        break
                    batch_id = claim_batch(
                        conn, cutoff, network, partition, settings.batch_max_rows
                    )
                    if batch_id:
                        found = True
                        claimed += 1
                        built += build_batch(conn, s3, settings.bucket, batch_id)
            if not found or claimed >= settings.schedule_max_batches:
                break
        remains = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM chargebacks "
            "WHERE batch_id IS NULL AND received_at < %s) AS yes",
            (cutoff,),
        ).fetchone()["yes"]
        if not remains:
            conn.execute(
                "UPDATE slots SET completed_at=clock_timestamp() WHERE cutoff=%s", (cutoff,)
            )
        return {"claimed": claimed, "built": built, "complete": not remains}
