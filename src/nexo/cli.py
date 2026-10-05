"""One-off administrative commands; credentials remain outside version control."""

import argparse
import hashlib
import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql


def migrate():
    directory = Path(os.getenv("MIGRATIONS_DIR", "migrations"))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817203,2)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY,"
            "sha256 text NOT NULL, applied_at timestamptz DEFAULT clock_timestamp())"
        )
        for path in sorted(directory.glob("*.sql")):
            body = path.read_text()
            digest = hashlib.sha256(body.encode()).hexdigest()
            existing = conn.execute(
                "SELECT sha256 FROM schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError(f"applied migration changed: {path.name}")
                continue
            conn.execute(body)
            conn.execute(
                "INSERT INTO schema_migrations(name,sha256) VALUES (%s,%s)", (path.name, digest)
            )
        role = os.getenv("NEXO_APP_DB_ROLE")
        if role:
            identifier = sql.Identifier(role)
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(identifier))
            conn.execute(
                sql.SQL("GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO {}").format(
                    identifier
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    identifier
                )
            )
            conn.execute(
                sql.SQL("REVOKE INSERT,UPDATE ON schema_migrations FROM {}").format(identifier)
            )


def issue_token(conn, role, customer=None, days=7):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO tokens(hash,customer_id,role,expires_at) VALUES (%s,%s,%s,%s)",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            customer,
            role,
            datetime.now(UTC) + timedelta(days=days),
        ),
    )
    return token


def seed(count=12):
    if not 1 <= count <= 100000:
        raise ValueError("seed count must be 1..100000")
    result = {"synthetic": True, "customers": []}
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for person in ("alice", "bob"):
            customer = uuid5(NAMESPACE_URL, f"nexo:synthetic:{person}")
            conn.execute(
                "INSERT INTO customers(id,label) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (customer, f"Synthetic {person.title()}"),
            )
            ids = []
            with conn.cursor() as cursor:
                values = []
                for n in range(count):
                    transaction = uuid5(customer, f"transaction:{n}")
                    ids.append(str(transaction))
                    values.append(
                        (
                            transaction,
                            customer,
                            "visa" if n % 2 == 0 else "mastercard",
                            "USD",
                            10000 + n,
                        )
                    )
                cursor.executemany(
                    "INSERT INTO transactions(id,customer_id,network,currency,amount_minor) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    values,
                )
            result["customers"].append(
                {
                    "id": str(customer),
                    "label": person,
                    "token": issue_token(conn, "customer", customer),
                    "transaction_ids": ids,
                }
            )
        result["ops_token"] = issue_token(conn, "ops")
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="nexo")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    scheduling = sub.add_parser("schedule")
    scheduling.add_argument(
        "--cutoff", help="ISO 8601 six-hour UTC boundary; defaults to latest due slot"
    )
    seeding = sub.add_parser("seed")
    seeding.add_argument("--count", type=int, default=12)
    issuing = sub.add_parser("token")
    issuing.add_argument("--role", choices=["customer", "ops"], required=True)
    issuing.add_argument("--customer-id", type=UUID)
    issuing.add_argument("--days", type=int, default=7)
    revoking = sub.add_parser("revoke-token")
    revoking.add_argument("--hash", required=True, help="SHA-256 token hash, never the raw token")
    sub.add_parser("simulator")
    args = parser.parse_args()
    if args.command == "migrate":
        migrate()
    elif args.command == "schedule":
        from nexo.batching import schedule
        from nexo.config import Settings

        cutoff = datetime.fromisoformat(args.cutoff) if args.cutoff else None
        print(json.dumps(schedule(Settings.from_env(), cutoff)))
    elif args.command == "seed":
        print(json.dumps(seed(args.count)))
    elif args.command == "token":
        if (args.role == "customer") != (args.customer_id is not None) or not 1 <= args.days <= 30:
            parser.error("customer role needs customer-id; token duration must be 1..30 days")
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            print(issue_token(conn, args.role, args.customer_id, args.days))
    elif args.command == "revoke-token":
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            result = conn.execute("UPDATE tokens SET revoked=true WHERE hash=%s", (args.hash,))
            print(json.dumps({"revoked": result.rowcount}))
    elif args.command == "simulator":
        from nexo.simulator import serve

        serve()


if __name__ == "__main__":
    main()
