"""Validate local explorer notes and update their fixed Markdown queue under one lock."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PANELS = {"architecture", "sequence", "code", "community", "glossary", "page"}
ENTRY = re.compile(r"^- \[[ x~?]\] (A-\d+) · .*$", re.MULTILINE)
RECEIPT = re.compile(r"^  <!-- nexo-request ([0-9a-f-]{36}) sha256:([0-9a-f]{64}) -->$", re.M)
HEADER = "# Annotation queue\n\nRules are in [AGENTS.md](../AGENTS.md#annotation-queue).\n"


class Conflict(ValueError):
    pass


def text_field(value: object, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text.")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(value) > limit or (required and not value.strip()):
        raise ValueError(f"Text must contain {'1' if required else '0'}–{limit} characters.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("Text contains an unsupported control character.")
    value.encode("utf-8")
    return value


def validate_note(payload: object, mapping: dict) -> tuple[dict, str, list[str]]:
    keys = {"request_id", "view", "selected_text", "targets", "note"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("Expected request_id, view, selected_text, targets and note.")
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("request_id must be a canonical UUID.")
    view = payload["view"]
    flows = {flow["id"]: flow["name"] for flow in mapping["flows"]}
    if (
        not isinstance(view, dict)
        or set(view) != {"flow_id", "panel"}
        or not isinstance(view["flow_id"], str)
        or view["flow_id"] not in flows
        or not isinstance(view["panel"], str)
        or view["panel"] not in PANELS
    ):
        raise ValueError("Choose a known explorer flow and panel.")
    lookup = {
        "component": {item["id"]: item["name"] for item in mapping["components"]},
        "arrow": {item["id"]: item["label"] for item in mapping["arrows"]},
        "message": {
            item["id"]: item["label"] for flow in mapping["flows"] for item in flow["messages"]
        },
    }
    targets = payload["targets"]
    if not isinstance(targets, list) or len(targets) > 8:
        raise ValueError("Choose at most eight targets.")
    normalized, labels, seen = [], [], set()
    for target in targets:
        if not isinstance(target, dict) or not {"kind", "id"} <= set(target) <= {
            "kind",
            "id",
            "label",
        }:
            raise ValueError("Target needs a kind and ID.")
        kind, identifier = target["kind"], target["id"]
        if (
            not isinstance(kind, str)
            or kind not in lookup
            or not isinstance(identifier, str)
            or identifier not in lookup[kind]
            or (kind, identifier) in seen
        ):
            raise ValueError("Choose distinct mapped targets.")
        if "label" in target:
            text_field(target["label"], 200)
        seen.add((kind, identifier))
        normalized.append({"kind": kind, "id": identifier})
        labels.append(
            f"{kind} {json.dumps(lookup[kind][identifier], ensure_ascii=False)} ({identifier})"
        )
    selected = text_field(payload["selected_text"], 2000)
    note = text_field(payload["note"], 4000, required=True)
    if not selected.strip() and not targets:
        raise ValueError("Select some text or a diagram target before saving.")
    result = dict(payload, view=dict(view), selected_text=selected, targets=normalized, note=note)
    return result, flows[view["flow_id"]], labels


def regular_file(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("Queue and lock must be regular files with one link.")
    return info


class AnnotationQueue:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()

    @contextmanager
    def locked(self):
        directory = os.open(
            self.root.parent / "feedback", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            lock = os.open(
                ".annotations.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            try:
                regular_file(lock)
                # The separate lock survives queue replacement, serializing appends and agent edits.
                fcntl.flock(lock, fcntl.LOCK_EX)
                descriptor = os.open(
                    "ANNOTATIONS.md",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "r", encoding="utf-8") as queue:
                    info = regular_file(queue.fileno())
                    current = queue.read()
                    yield directory, info, current, queue.fileno()
            finally:
                os.close(lock)
        finally:
            os.close(directory)

    def replace(self, directory: int, before: os.stat_result, content: str) -> None:
        temporary = f".annotations-{uuid4()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(before.st_mode),
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            now = os.stat("ANNOTATIONS.md", dir_fd=directory, follow_symlinks=False)
            if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                raise OSError("The queue changed outside its lock; retry the operation.")
            os.replace(temporary, "ANNOTATIONS.md", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def append(self, payload: object) -> dict:
        mapping = json.loads((self.root / "explorer/mapping.json").read_text())
        note, flow_name, labels = validate_note(payload, mapping)
        canonical = json.dumps(note, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.locked() as (directory, before, current, descriptor):
            entries = list(ENTRY.finditer(current))
            for receipt in RECEIPT.finditer(current):
                if receipt[1] == note["request_id"]:
                    if receipt[2] != digest:
                        raise Conflict("This request ID already saved different content.")
                    entry = next(
                        (item for item in reversed(entries) if item.start() < receipt.start()), None
                    )
                    if entry is None:
                        raise OSError("Annotation receipt has no entry.")
                    os.fsync(descriptor)
                    os.fsync(directory)
                    return {"id": entry[1], "duplicate": True}
            identifier = f"A-{max((int(item[1][2:]) for item in entries), default=0) + 1:03d}"
            timestamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")
            selected = json.dumps(note["selected_text"], ensure_ascii=False)
            targets = " · ".join(([selected] if note["selected_text"] else []) + labels)
            body = "\n".join("  > " + line for line in note["note"].split("\n"))
            entry = (
                f"- [ ] {identifier} · {timestamp} · Explorer › {flow_name}"
                f" › {note['view']['panel']}\n"
                f"  Target: {targets}\n{body}\n"
                f"  <!-- nexo-request {note['request_id']} sha256:{digest} -->\n"
            )
            prefix = current or HEADER
            separator = "\n" if prefix.endswith("\n") else "\n\n"
            self.replace(directory, before, prefix + separator + entry)
            return {"id": identifier, "duplicate": False}

    def update(self, identifier: str, status: str, **fields: str | None) -> None:
        if not re.fullmatch(r"A-\d+", identifier) or status not in {" ", "~", "x", "?"}:
            raise ValueError("Choose an existing A-ID and a valid status.")
        if set(fields) - {"Answer", "Done", "Question"}:
            raise ValueError("Only Answer, Done and Question lines can be updated.")
        fields = {
            key: text_field(value, 8000, True) for key, value in fields.items() if value is not None
        }
        with self.locked() as (directory, before, current, _):
            entries = list(ENTRY.finditer(current))
            matching = [item for item in entries if item[1] == identifier]
            if len(matching) != 1:
                raise ValueError("Expected one existing annotation with that ID.")
            match = matching[0]
            end = next(
                (item.start() for item in entries if item.start() > match.start()), len(current)
            )
            block = current[match.start() : end]
            block = block[:3] + status + block[4:]
            for name, value in fields.items():
                pattern = re.compile(rf"^  {name}:.*(?:\n    [^\n]*)*\n?", re.M)
                replacement = f"  {name}: " + value.replace("\n", "\n    ") + "\n"
                if pattern.search(block):
                    block = pattern.sub(lambda _, value=replacement: value, block, count=1)
                else:
                    receipt = RECEIPT.search(block)
                    point = receipt.start() if receipt else len(block.rstrip("\n"))
                    separator = "" if point == 0 or block[point - 1] == "\n" else "\n"
                    block = block[:point] + separator + replacement + block[point:]
            self.replace(directory, before, current[: match.start()] + block + current[end:])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", help="Existing annotation ID, such as A-001")
    parser.add_argument(
        "--status", required=True, choices=["waiting", "in-progress", "done", "blocked"]
    )
    for field in ("answer", "done", "question"):
        parser.add_argument(f"--{field}-file", type=Path)
    args = parser.parse_args()
    statuses = dict(
        zip(["waiting", "in-progress", "done", "blocked"], [" ", "~", "x", "?"], strict=True)
    )
    fields = {
        name.title(): path.read_text().rstrip("\n") if path else None
        for name in ("answer", "done", "question")
        if (path := getattr(args, f"{name}_file")) is not None
    }
    AnnotationQueue().update(args.id, statuses[args.status], **fields)
    print(f"Updated {args.id}.")


if __name__ == "__main__":
    main()
