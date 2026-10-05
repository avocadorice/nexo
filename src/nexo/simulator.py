"""A separate simulated card network speaking real SFTP, with durable receipts."""

import csv
import errno
import hashlib
import io
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path, PurePosixPath
from uuid import UUID

import paramiko

from nexo.batching import HEADER

log = logging.getLogger(__name__)
MAX_BYTES = 4 * 1024 * 1024
ACTIVE_UPLOADS = set()
UPLOAD_LOCK = threading.Lock()


def mode(root):
    try:
        return json.loads((root / "mode.json").read_text())
    except (FileNotFoundError, ValueError):
        return {"mode": "normal"}


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Receiver:
    def __init__(self, root):
        self.root = Path(root)
        for name in ("incoming", "acks"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS receipts(batch_id TEXT PRIMARY KEY,sha256 TEXT,
                    row_count INTEGER,status TEXT,reason TEXT,ready_at REAL,
                    emitted INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS members(chargeback_id TEXT PRIMARY KEY,batch_id TEXT);
            """)

    def database(self):
        db = sqlite3.connect(self.root / "receipts.sqlite", timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def ingest(self, path):
        batch_id = str(UUID(path.stem))
        with self.database() as db:
            if db.execute("SELECT 1 FROM receipts WHERE batch_id=?", (batch_id,)).fetchone():
                return
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            raise ValueError("oversized file")
        digest = hashlib.sha256(data).hexdigest()
        options = mode(self.root)
        rows, reason = [], None
        try:
            reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
            if reader.fieldnames != HEADER:
                raise ValueError("invalid header")
            rows = list(reader)
            if not 1 <= len(rows) <= 10000:
                raise ValueError("invalid row count")
            networks = set()
            for row in rows:
                if list(row) != HEADER or any(value is None for value in row.values()):
                    raise ValueError("invalid CSV columns")
                for field in ("chargeback_id", "transaction_id"):
                    if str(UUID(row[field])) != row[field]:
                        raise ValueError("noncanonical UUID")
                networks.add(row["network"])
                if (
                    row["network"] not in ("visa", "mastercard")
                    or not row["amount_minor"].isascii()
                    or not row["amount_minor"].isdigit()
                ):
                    raise ValueError("invalid row")
                if not 0 < int(row["amount_minor"]) <= 9223372036854775807:
                    raise ValueError("invalid amount")
                if len(row["currency"]) != 3 or not all(
                    "A" <= char <= "Z" for char in row["currency"]
                ):
                    raise ValueError("invalid currency")
                if row["reason"] not in ("FRAUD", "DUPLICATE", "NOT_RECEIVED", "OTHER"):
                    raise ValueError("invalid reason")
            if len(networks) != 1:
                raise ValueError("mixed networks")
            if options["mode"] == "reject":
                raise ValueError("simulated rejection")
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            reason = str(error)
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT sha256 FROM receipts WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    log.error("receiver_file_conflict batch_id=%s", batch_id)
                return
            if reason is None:
                # This transaction is the receiver's deduplication boundary, not an SFTP guarantee.
                db.execute("SAVEPOINT members")
                try:
                    db.executemany(
                        "INSERT INTO members VALUES (?,?)",
                        [(r["chargeback_id"], batch_id) for r in rows],
                    )
                except sqlite3.IntegrityError:
                    db.execute("ROLLBACK TO members")
                    reason = "duplicate chargeback identity"
                db.execute("RELEASE members")
            delay = (
                max(0, min(float(options.get("delay_seconds", 30)), 86400))
                if options["mode"] == "late_ack"
                else 0
            )
            db.execute(
                "INSERT INTO receipts(batch_id,sha256,row_count,status,reason,ready_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    batch_id,
                    digest,
                    len(rows),
                    "rejected" if reason else "accepted",
                    reason,
                    time.time() + delay,
                ),
            )
        log.info(
            "receiver_recorded batch_id=%s status=%s",
            batch_id,
            "rejected" if reason else "accepted",
        )

    def tick(self):
        # Connections last at most 45 seconds. Old abandoned temps are never final evidence.
        for temp in (self.root / "incoming").glob(".*.tmp"):
            with UPLOAD_LOCK:
                if temp not in ACTIVE_UPLOADS and temp.stat().st_mtime < time.time() - 3600:
                    temp.unlink(missing_ok=True)
        for path in (self.root / "incoming").glob("*.csv"):
            try:
                self.ingest(path)
            except (OSError, ValueError, sqlite3.Error):
                log.exception("receiver_invalid_file")
        if mode(self.root)["mode"] == "missing_ack":
            return
        with self.database() as db:
            receipts = db.execute(
                "SELECT batch_id,sha256,row_count,status,reason,ready_at FROM receipts "
                "WHERE emitted=0 AND ready_at<=? LIMIT 200",
                (time.time(),),
            ).fetchall()
        for batch_id, digest, count, status, reason, _ in receipts:
            final = self.root / "acks" / f"{batch_id}.json"
            if final.exists():
                with self.database() as db:
                    db.execute("UPDATE receipts SET emitted=1 WHERE batch_id=?", (batch_id,))
                continue
            data = {
                "version": 1,
                "batch_id": batch_id,
                "sha256": digest,
                "row_count": count,
                "status": status,
                "reason": reason,
            }
            temp = final.with_suffix(".tmp")
            with temp.open("w") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, final)
            fsync_directory(final.parent)
            with self.database() as db:
                db.execute("UPDATE receipts SET emitted=1 WHERE batch_id=?", (batch_id,))


class SSHAuth(paramiko.ServerInterface):
    def __init__(self, username, password):
        self.username, self.password = username, password

    def check_auth_password(self, username, password):
        import hmac

        if hmac.compare_digest(username, self.username) and hmac.compare_digest(
            password, self.password
        ):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


class UploadHandle(paramiko.SFTPHandle):
    def __init__(self, flags, root, target=None):
        super().__init__(flags)
        self.root, self.target = root, target

    def write(self, offset, data):
        if offset + len(data) > MAX_BYTES:
            return paramiko.SFTP_FAILURE
        selected = mode(self.root)["mode"]
        if selected == "timeout":
            time.sleep(15)
            return paramiko.SFTP_FAILURE
        if selected == "partial_upload":
            super().write(offset, data[:16])
            return paramiko.SFTP_FAILURE
        return super().write(offset, data)

    def close(self):
        if hasattr(self, "writefile"):
            self.writefile.flush()
            os.fsync(self.writefile.fileno())
        result = super().close()
        if self.target is not None:
            with UPLOAD_LOCK:
                ACTIVE_UPLOADS.discard(self.target)
        return result


class SFTPFiles(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root, transport, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root, self.transport = Path(root), transport

    def resolve(self, path):
        parts = PurePosixPath(path).parts
        if (
            ".." in parts
            or len(parts) != 3
            or parts[0] != "/"
            or parts[1] not in ("incoming", "acks")
        ):
            raise PermissionError("invalid path")
        target = self.root / parts[1] / parts[2]
        if target.is_symlink():
            raise PermissionError("symlinks forbidden")
        return target

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.resolve(path).stat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or errno.EACCES)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            target = self.resolve(path)
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and (
                target.parent.name != "incoming"
                or not target.name.startswith(".")
                or not target.name.endswith(".tmp")
            ):
                return paramiko.SFTP_PERMISSION_DENIED
            if writing:
                flags |= os.O_EXCL | os.O_CREAT
            with UPLOAD_LOCK:
                fd = os.open(target, flags, 0o600)
                if writing:
                    ACTIVE_UPLOADS.add(target)
            handle = UploadHandle(flags, self.root, target if writing else None)
            if writing:
                handle.writefile = os.fdopen(fd, "wb")
            else:
                handle.readfile = os.fdopen(fd, "rb")
            return handle
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or errno.EACCES)

    def rename(self, oldpath, newpath):
        try:
            old, new = self.resolve(oldpath), self.resolve(newpath)
            if (
                old.parent.name != "incoming"
                or new.parent != old.parent
                or not old.name.startswith(".")
                or not old.name.endswith(".tmp")
                or not new.name.endswith(".csv")
            ):
                return paramiko.SFTP_PERMISSION_DENIED
            UUID(new.stem)
            # link() atomically refuses an existing final name. Published bytes never change.
            with UPLOAD_LOCK:
                if old in ACTIVE_UPLOADS:
                    return paramiko.SFTP_FAILURE
                os.link(old, new)
                fsync_directory(new.parent)
                old.unlink()
            if mode(self.root)["mode"] == "disconnect_after_rename":
                self.transport.close()
            return paramiko.SFTP_OK
        except ValueError:
            return paramiko.SFTP_PERMISSION_DENIED
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or errno.EACCES)

    def remove(self, path):
        try:
            target = self.resolve(path)
            if target.parent.name != "incoming" or not target.name.endswith(".tmp"):
                return paramiko.SFTP_PERMISSION_DENIED
            target.unlink()
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or errno.EACCES)


def serve():
    logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)
    root = Path(os.getenv("SIM_ROOT", "/data"))
    receiver = Receiver(root)
    key = paramiko.RSAKey.from_private_key_file(os.environ["SIM_HOST_KEY"])
    username, password = os.environ["SFTP_USER"], os.environ["SFTP_PASSWORD"]
    if len(password) < 24:
        raise ValueError("SFTP_PASSWORD must have at least 24 characters")
    if not (root / "mode.json").exists():
        (root / "mode.json").write_text(json.dumps({"mode": os.getenv("SIM_MODE", "normal")}))

    def accept_client(client):
        transport = paramiko.Transport(client)
        try:
            transport.add_server_key(key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer, SFTPFiles, root=root, transport=transport
            )
            transport.start_server(server=SSHAuth(username, password))
            while transport.is_active():
                time.sleep(0.1)
        except (EOFError, OSError, paramiko.SSHException):
            log.info("sftp_connection_closed")
        finally:
            transport.close()

    def receive_loop():
        while True:
            try:
                receiver.tick()
            except Exception:
                log.exception("receiver_tick_failed")
            time.sleep(1)

    threading.Thread(target=receive_loop, daemon=True).start()
    with socket.create_server(
        (os.getenv("SIM_HOST", "0.0.0.0"), int(os.getenv("SIM_PORT", "2222")))
    ) as server:
        while True:
            client, _ = server.accept()
            client.settimeout(30)
            threading.Thread(target=accept_client, args=(client,), daemon=True).start()
