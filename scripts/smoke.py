"""Verify the real local API and optionally advance synthetic due fixtures through Kubernetes."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
DUE_FIXTURE = '''
import hashlib,json,os
from datetime import UTC,datetime,timedelta
from uuid import uuid4
import psycopg
from nexo.batching import slot_cutoff
cutoff=slot_cutoff(datetime.now(UTC))
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    customer=conn.execute("SELECT id FROM customers WHERE label='Synthetic Alice'").fetchone()[0]
    prior=conn.execute("""SELECT id FROM chargebacks WHERE customer_id=%s
        AND idempotency_key LIKE 'smoke-%%' AND batch_id IS NOT NULL
        ORDER BY received_at,id LIMIT 8""",(customer,)).fetchall()
    if len(prior)==8:
        print(json.dumps({"due_chargeback_ids":[str(row[0]) for row in prior],
                          "cutoff":cutoff.isoformat(),"reused":True}))
        raise SystemExit(0)
    completed=conn.execute("SELECT completed_at FROM slots WHERE cutoff=%s",(cutoff,)).fetchone()
    if completed and completed[0] is not None:
        raise RuntimeError("Current slot already completed; run due-fixture smoke in a fresh slot")
    ids=[]
    for n in range(8):
        transaction,chargeback=uuid4(),uuid4()
        conn.execute("INSERT INTO transactions VALUES (%s,%s,%s,'USD',2500)",
                     (transaction,customer,'visa' if n%2==0 else 'mastercard'))
        conn.execute("""INSERT INTO chargebacks
            (id,customer_id,transaction_id,idempotency_key,request_hash,
             amount_minor,reason,received_at,partition)
            VALUES (%s,%s,%s,%s,%s,2500,'FRAUD',%s,%s)""",
            (chargeback,customer,transaction,'smoke-'+str(chargeback),
             hashlib.sha256(str(chargeback).encode()).hexdigest(),
             cutoff-timedelta(seconds=1),n//2))
        ids.append(str(chargeback))
print(json.dumps({"due_chargeback_ids":ids,"cutoff":cutoff.isoformat()}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--seed-file", type=Path, default=ROOT / ".local/seed.json")
    parser.add_argument("--context", default="kind-nexo")
    parser.add_argument("--with-due-fixtures", action="store_true")
    args = parser.parse_args()
    seed = json.loads(args.seed_file.read_text())
    first, second = seed["customers"][:2]
    report = {}
    with httpx.Client(base_url=args.base_url, timeout=15) as client:
        headers = {"Authorization": "Bearer " + first["token"]}
        txs = client.get("/api/transactions", headers=headers).json()["transactions"]
        transaction = next(row for row in txs if not row["disputed"])
        body = {"transaction_id": transaction["id"], "amount_minor": "1234", "reason": "FRAUD"}
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post("/api/chargebacks", headers=headers, json=body)
        assert response.status_code == 201, response.text
        chargeback = response.json()
        replay = client.post("/api/chargebacks", headers=headers, json=body)
        assert replay.status_code == 200 and replay.json()["id"] == chargeback["id"]
        assert (
            client.get(
                "/api/chargebacks/" + chargeback["id"],
                headers={
                    "Authorization": "Bearer " + second["token"],
                },
            ).status_code
            == 404
        )
        report["api"] = {"created": chargeback["id"], "replay": "same ID", "cross_customer": 404}
        if args.with_due_fixtures:
            environment = dict(os.environ)
            environment.setdefault("KUBECONFIG", str(ROOT / ".local/kubeconfig"))
            kubectl = ["kubectl", "--context", args.context, "-n", "nexo"]
            result = subprocess.run(
                kubectl + ["exec", "-i", "deployment/api", "--", "python", "-"],
                input=DUE_FIXTURE,
                capture_output=True,
                text=True,
                env=environment,
                check=True,
            )
            fixture = json.loads(result.stdout)
            job = "smoke-slot-" + uuid4().hex[:8]
            subprocess.run(
                kubectl + ["create", "job", job, "--from=cronjob/schedule"],
                check=True,
                capture_output=True,
                env=environment,
            )
            subprocess.run(
                kubectl + ["wait", "--for=condition=complete", "job/" + job, "--timeout=60s"],
                check=True,
                capture_output=True,
                env=environment,
            )
            deadline = time.monotonic() + 90
            states = []
            while time.monotonic() < deadline:
                states = [
                    client.get("/api/chargebacks/" + identity, headers=headers).json()
                    for identity in fixture["due_chargeback_ids"]
                ]
                if all(row["status"] == "acknowledged" for row in states):
                    break
                time.sleep(2)
            assert all(row["status"] == "acknowledged" for row in states), states
            report["pipeline"] = {
                "fixture": "synthetic rows received before cutoff",
                "acknowledged": len(states),
                "job": job,
                "batch_ids": sorted({row["batch_id"] for row in states}),
            }
        ops = {"Authorization": "Bearer " + seed["ops_token"]}
        report["operations"] = client.get("/api/ops/summary", headers=ops).json()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
