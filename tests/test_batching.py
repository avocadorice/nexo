import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from nexo.batching import build_batch, claim_batch, slot_cutoff


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL integration")
    schema = "batch_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    with connect() as conn:
        conn.execute(Path("migrations/001_initial.sql").read_text())
    yield connect
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def populate(connect, count=40):
    cutoff = slot_cutoff(datetime.now(UTC))
    customer = uuid4()
    with connect() as conn:
        conn.execute("INSERT INTO customers VALUES (%s,'Synthetic test')", (customer,))
        conn.execute("INSERT INTO slots(cutoff) VALUES (%s)", (cutoff,))
        for n in range(count):
            transaction = uuid4()
            conn.execute(
                "INSERT INTO transactions VALUES (%s,%s,'visa','USD',9007199254740993)",
                (transaction, customer),
            )
            conn.execute(
                """INSERT INTO chargebacks
                (id,customer_id,transaction_id,idempotency_key,request_hash,amount_minor,reason,received_at,partition)
                VALUES (%s,%s,%s,%s,%s,9007199254740993,'FRAUD',%s,0)""",
                (uuid4(), customer, transaction, str(n), "a" * 64, cutoff - timedelta(seconds=1)),
            )
    return cutoff


def test_parallel_claims_never_reassign(database):
    cutoff = populate(database)

    def claim(_):
        ids = []
        with database() as conn:
            while batch := claim_batch(conn, cutoff, "visa", 0, 3):
                ids.append(batch)
        return ids

    with ThreadPoolExecutor(max_workers=8) as executor:
        batches = [batch for group in executor.map(claim, range(8)) for batch in group]
    with database() as conn:
        rows = conn.execute(
            "SELECT batch_id,count(*) n FROM chargebacks GROUP BY batch_id"
        ).fetchall()
        assert all(row["batch_id"] is not None and row["n"] <= 3 for row in rows)
        assert sum(row["n"] for row in rows) == 40
        assert len(rows) == len(set(batches))
        assert claim_batch(conn, cutoff, "visa", 0, 3) is None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE chargebacks SET batch_id=NULL")


class MemoryObjectStore:
    def __init__(self):
        self.objects = {}
        self.fail_once = True

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("lost S3 response after object persisted")


def test_rebuild_after_ambiguous_object_write_is_identical(database):
    cutoff = populate(database, 5)
    s3 = MemoryObjectStore()
    with database() as conn:
        batch = claim_batch(conn, cutoff, "visa", 0, 10)
        with pytest.raises(TimeoutError):
            build_batch(conn, s3, "test", batch)
        original = dict(s3.objects)
        assert (
            conn.execute("SELECT state FROM batches WHERE id=%s", (batch,)).fetchone()["state"]
            == "building"
        )
        assert build_batch(conn, s3, "test", batch)
        assert s3.objects == original
        assert b"9007199254740993" in next(iter(original.values()))
        ready = conn.execute("SELECT * FROM batches WHERE id=%s", (batch,)).fetchone()
        assert ready["sha256"] == hashlib.sha256(next(iter(original.values()))).hexdigest()
        assert not build_batch(conn, s3, "test", batch)


def test_utc_slots_and_naive_datetime():
    assert slot_cutoff(datetime(2026, 1, 1, 17, 59, tzinfo=UTC)).hour == 12
    with pytest.raises(ValueError):
        slot_cutoff(datetime(2026, 1, 1))
