import json
from uuid import uuid4

from nexo.batching import csv_bytes
from nexo.simulator import Receiver


def write_file(receiver, chargeback=None):
    batch = str(uuid4())
    data = csv_bytes(
        [
            {
                "chargeback_id": chargeback or uuid4(),
                "transaction_id": uuid4(),
                "network": "visa",
                "currency": "USD",
                "amount_minor": 9007199254740993,
                "reason": "FRAUD",
            }
        ]
    )
    (receiver.root / "incoming" / f"{batch}.csv").write_bytes(data)
    return batch


def test_receiver_persists_deduplication_across_restart(tmp_path):
    receiver = Receiver(tmp_path)
    chargeback = uuid4()
    batch = write_file(receiver, chargeback)
    receiver.tick()
    ack = json.loads((tmp_path / "acks" / f"{batch}.json").read_text())
    assert ack["status"] == "accepted"
    restarted = Receiver(tmp_path)
    duplicate = write_file(restarted, chargeback)
    restarted.tick()
    assert json.loads((tmp_path / "acks" / f"{duplicate}.json").read_text())["status"] == "rejected"
    with restarted.database() as db:
        assert db.execute("SELECT count(*) FROM members").fetchone()[0] == 1


def test_missing_and_late_acknowledgements(tmp_path):
    receiver = Receiver(tmp_path)
    (tmp_path / "mode.json").write_text(json.dumps({"mode": "missing_ack"}))
    batch = write_file(receiver)
    receiver.tick()
    assert not (tmp_path / "acks" / f"{batch}.json").exists()
    (tmp_path / "mode.json").write_text(json.dumps({"mode": "normal"}))
    receiver.tick()
    assert (tmp_path / "acks" / f"{batch}.json").exists()
    (tmp_path / "mode.json").write_text(json.dumps({"mode": "late_ack", "delay_seconds": 300}))
    late = write_file(receiver)
    receiver.tick()
    assert not (tmp_path / "acks" / f"{late}.json").exists()
    with receiver.database() as db:
        assert (
            db.execute("SELECT status FROM receipts WHERE batch_id=?", (late,)).fetchone()[0]
            == "accepted"
        )
