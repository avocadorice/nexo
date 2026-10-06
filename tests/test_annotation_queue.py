"""Real local HTTP and file operations protect note identity and concurrent queue edits."""

import errno
import hashlib
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
annotations = importlib.import_module("annotation_queue")
helper = importlib.import_module("editor_helper")


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "nexo"
    (root / "explorer").mkdir(parents=True)
    (tmp_path / "feedback").mkdir()
    (tmp_path / "feedback/ANNOTATIONS.md").write_text("# Annotation queue\n\nExisting preface.\n")
    (root / "sample.py").write_text("def save():\n    return True\n")
    source = {"file": "sample.py", "symbol": "save"}
    mapping = {
        "components": [{"id": "db", "name": "PostgreSQL", "sources": [source]}],
        "arrows": [{"id": "commit", "label": "Commit row"}],
        "flows": [
            {
                "id": "create",
                "name": "Create a chargeback",
                "messages": [{"id": "create-db", "label": "Save request", "sources": [source]}],
            }
        ],
    }
    (root / "explorer/mapping.json").write_text(json.dumps(mapping))
    return root


@pytest.fixture
def note():
    return {
        "request_id": str(uuid4()),
        "view": {"flow_id": "create", "panel": "sequence"},
        "selected_text": "Records survive retries.",
        "targets": [{"kind": "component", "id": "db"}],
        "note": "Why is this persisted here?",
    }


@pytest.fixture
def drawn_note(note):
    return dict(
        note,
        selected_text="",
        targets=[],
        note="Move this control [arrow 2] here. Why does [arrow 7] persist? Compare [arrow 2].",
        arrows=[
            {
                "id": 2,
                "from": {"panel": "code", "text": "Open in VS Code", "x": 0.1, "y": 0.2},
                "to": {"panel": "code", "text": "", "x": 1, "y": 0},
            },
            {
                "id": 7,
                "from": {
                    "panel": "sequence",
                    "target": {"kind": "message", "id": "create-db"},
                    "text": "Save request",
                    "x": 0.5,
                    "y": 0.25,
                },
                "to": {
                    "panel": "architecture",
                    "target": {"kind": "component", "id": "db"},
                    "text": "PostgreSQL",
                    "x": 0.75,
                    "y": 0.5,
                },
            },
        ],
    )


def queue_path(root):
    return root.parent / "feedback/ANNOTATIONS.md"


def test_append_quotes_every_line_and_deduplicates_without_changing_user_words(checkout, note):
    queue = annotations.AnnotationQueue(checkout)
    note["selected_text"] = 'a "quote"\n- [ ] A-900 · fake'
    note["note"] = "Why?\r\n\r\n- [ ] A-999 · fake\n  <!-- nexo-request fake -->"
    note["targets"][0]["label"] = "untrusted client label"
    result = queue.append(note)
    saved = queue_path(checkout).read_text()
    assert result == {"id": "A-001", "duplicate": False}
    assert saved.startswith("# Annotation queue\n\nExisting preface.\n")
    assert 'component "PostgreSQL" (db)' in saved
    assert "untrusted client label" not in saved
    assert "  > Why?\n  > \n  > - [ ] A-999 · fake\n  >   <!-- nexo-request fake -->" in saved
    assert len(list(annotations.ENTRY.finditer(saved))) == 1
    assert len(list(annotations.RECEIPT.finditer(saved))) == 1
    assert queue.append(note) == {"id": "A-001", "duplicate": True}
    assert queue_path(checkout).read_text() == saved
    changed = dict(note, note="Different question")
    with pytest.raises(annotations.Conflict):
        queue.append(changed)
    assert queue_path(checkout).read_text() == saved


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "not a uuid"},
        {"request_id": 123},
        {"view": {"flow_id": "missing", "panel": "page"}},
        {"view": {"flow_id": "create", "panel": "unknown"}},
        {"view": {"flow_id": "create", "panel": []}},
        {"view": {"flow_id": "create", "panel": "page", "path": "/tmp/notes"}},
        {"targets": [{"kind": "component", "id": "unknown"}]},
        {"targets": [{"kind": "path", "id": "/tmp/notes"}]},
        {"targets": [{"kind": "component", "id": "db", "label": 4}]},
        {"targets": [{"kind": "component", "id": "db", "extra": True}]},
        {"targets": [{"kind": "component", "id": "db"}] * 2},
        {"targets": [], "selected_text": "  "},
        {"targets": [None]},
        {"selected_text": "x" * 2001},
        {"note": "x" * 4001},
        {"note": "  \n"},
        {"note": "bad\x00text"},
        {"note": "bad\ud800text"},
        {"unknown": []},
    ],
)
def test_invalid_note_does_not_touch_queue(checkout, note, change):
    before = queue_path(checkout).read_bytes()
    with pytest.raises(ValueError):
        annotations.AnnotationQueue(checkout).append(dict(note, **change))
    assert queue_path(checkout).read_bytes() == before


def test_arrows_only_note_saves_both_endpoints_and_repeated_marker(checkout, drawn_note):
    queue = annotations.AnnotationQueue(checkout)
    assert queue.append(drawn_note) == {"id": "A-001", "duplicate": False}
    saved = queue_path(checkout).read_text()
    assert "  Target: Drawn arrows below\n" in saved
    assert (
        '  Arrow 2: from code · "Open in VS Code" · (x=0.1, y=0.2) → to code · (x=1, y=0)\n'
    ) in saved
    assert 'Arrow 7: from sequence · message "Save request" (create-db)' in saved
    assert '→ to architecture · component "PostgreSQL" (db)' in saved
    assert "  > " + drawn_note["note"] in saved
    assert queue.append(drawn_note) == {"id": "A-001", "duplicate": True}
    changed = deepcopy(drawn_note)
    changed["arrows"][0]["to"]["x"] = 0.3
    with pytest.raises(annotations.Conflict):
        queue.append(changed)
    assert queue_path(checkout).read_text() == saved


def test_endpoint_text_is_escaped_and_normalized_without_injecting_queue_entries(
    checkout, drawn_note
):
    queue = annotations.AnnotationQueue(checkout)
    endpoint = drawn_note["arrows"][0]["from"]
    endpoint["text"] = 'a "quote"\r\n- [ ] A-900 · fake\r\n  <!-- nexo-request fake -->'
    queue.append(drawn_note)
    saved = queue_path(checkout).read_text()
    assert len(list(annotations.ENTRY.finditer(saved))) == 1
    assert len(list(annotations.RECEIPT.finditer(saved))) == 1
    assert json.dumps(endpoint["text"].replace("\r\n", "\n"), ensure_ascii=False) in saved
    endpoint["text"] = endpoint["text"].replace("\r\n", "\n")
    drawn_note["arrows"][0]["to"]["x"] = 1.0
    assert queue.append(drawn_note)["duplicate"]


def test_pre_arrow_receipt_still_deduplicates_with_omitted_or_empty_arrows(checkout, note):
    canonical = json.dumps(note, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    original = (
        "- [x] A-001 · 2026-10-05 21:00 · Explorer › existing entry\n"
        "  > Existing note\n"
        f"  <!-- nexo-request {note['request_id']} sha256:{digest} -->\n"
    )
    queue_path(checkout).write_text(original)
    queue = annotations.AnnotationQueue(checkout)
    for payload in [note, dict(note, arrows=[])]:
        assert queue.append(payload) == {"id": "A-001", "duplicate": True}
    assert queue_path(checkout).read_text() == original


@pytest.mark.parametrize(
    "change",
    [
        {"arrows": None},
        {"arrows": {}},
        {"arrows": [None]},
        {"arrows": []},
        {"note": "No markers here"},
        {"note": "Only [arrow 2]"},
        {"note": "Unknown [arrow 2] [arrow 7] [arrow 8]"},
        {"note": "Malformed [arrow 02] [arrow 7]"},
        {"note": "Malformed [arrow 0] [arrow 2] [arrow 7]"},
        {"note": "Malformed [arrow -2] [arrow 7]"},
        {"note": "Malformed [arrow two] [arrow 7]"},
    ],
)
def test_arrow_markers_and_collection_reject_incomplete_context(checkout, drawn_note, change):
    before = queue_path(checkout).read_bytes()
    with pytest.raises(ValueError):
        annotations.AnnotationQueue(checkout).append(dict(drawn_note, **change))
    assert queue_path(checkout).read_bytes() == before


@pytest.mark.parametrize("identifier", [True, False, 0, -1, "2", 2.0, 7, None])
def test_arrow_ids_are_unique_positive_integers(checkout, drawn_note, identifier):
    drawn_note["arrows"][0]["id"] = identifier
    with pytest.raises(ValueError, match="Arrow IDs"):
        annotations.AnnotationQueue(checkout).append(drawn_note)


@pytest.mark.parametrize(
    "change",
    [
        {"panel": "unknown"},
        {"panel": []},
        {"text": "x" * 501},
        {"text": None},
        {"text": "bad\x00text"},
        {"text": "bad\ud800text"},
        {"x": True},
        {"x": "0.5"},
        {"x": -0.01},
        {"y": 1.01},
        {"x": float("nan")},
        {"y": float("inf")},
        {"x": float("-inf")},
        {"target": {"kind": "component", "id": "unknown"}},
        {"target": {"kind": "path", "id": "/etc/passwd"}},
        {"target": {"kind": "component", "id": "db", "label": "forged"}},
        {"target": None},
        {"path": "/tmp/arrows.svg"},
    ],
)
def test_arrow_endpoints_are_bounded_and_use_known_mapping_ids(checkout, drawn_note, change):
    drawn_note["arrows"][0]["from"].update(change)
    before = queue_path(checkout).read_bytes()
    with pytest.raises(ValueError):
        annotations.AnnotationQueue(checkout).append(drawn_note)
    assert queue_path(checkout).read_bytes() == before


def test_arrows_require_both_complete_endpoints_and_reject_unknown_fields(checkout, drawn_note):
    for field in ["from", "to", "id"]:
        invalid = deepcopy(drawn_note)
        del invalid["arrows"][0][field]
        with pytest.raises(ValueError):
            annotations.AnnotationQueue(checkout).append(invalid)
    for field in ["panel", "text", "x", "y"]:
        invalid = deepcopy(drawn_note)
        del invalid["arrows"][0]["to"][field]
        with pytest.raises(ValueError):
            annotations.AnnotationQueue(checkout).append(invalid)
    drawn_note["arrows"][0]["extra"] = "not accepted"
    with pytest.raises(ValueError):
        annotations.AnnotationQueue(checkout).append(drawn_note)


def test_at_most_eight_arrows_are_saved(checkout, drawn_note):
    drawn_note["arrows"] = [dict(drawn_note["arrows"][0], id=i) for i in range(1, 10)]
    drawn_note["note"] = " ".join(f"[arrow {i}]" for i in range(1, 10))
    with pytest.raises(ValueError, match="at most eight"):
        annotations.AnnotationQueue(checkout).append(drawn_note)


def test_concurrent_appends_and_agent_edits_preserve_all_entries(checkout, note):
    queue = annotations.AnnotationQueue(checkout)
    queue.append(note)
    second = dict(note, request_id=str(uuid4()), note="Keep this original note.")
    queue.append(second)
    saved_second = queue_path(checkout).read_text().split("- [ ] A-002", 1)[1]

    def work(index):
        local = annotations.AnnotationQueue(checkout)
        if index % 3 == 0:
            local.update("A-001", "x", Answer="The database makes this durable.", Done="Explained.")
            return None
        payload = dict(note, request_id=str(uuid4()), note=f"Question {index}")
        return local.append(payload)["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = [item for item in pool.map(work, range(30)) if item]
    saved = queue_path(checkout).read_text()
    assert len(ids) == len(set(ids)) == 20
    assert len(list(annotations.ENTRY.finditer(saved))) == 22
    assert "- [x] A-001" in saved
    assert saved.count("  Answer: The database makes this durable.") == 1
    assert "- [ ] A-002" + saved_second in saved
    assert queue.append(note) == {"id": "A-001", "duplicate": True}


@pytest.mark.parametrize("name", ["ANNOTATIONS.md", ".annotations.lock"])
def test_queue_or_lock_symlink_and_hard_link_cannot_redirect_writes(checkout, note, name):
    destination = checkout.parent / "feedback" / name
    outside = checkout.parent / "outside.txt"
    outside.write_text("Unchanged")
    destination.unlink(missing_ok=True)
    for create in [lambda: destination.symlink_to(outside), lambda: os.link(outside, destination)]:
        create()
        with pytest.raises(OSError):
            annotations.AnnotationQueue(checkout).append(note)
        assert outside.read_text() == "Unchanged"
        destination.unlink()


def test_feedback_directory_symlink_is_rejected(checkout, note):
    feedback = checkout.parent / "feedback"
    elsewhere = checkout.parent / "elsewhere"
    feedback.rename(elsewhere)
    feedback.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        annotations.AnnotationQueue(checkout).append(note)
    assert (elsewhere / "ANNOTATIONS.md").read_text().endswith("Existing preface.\n")


def test_replace_failure_leaves_queue_unchanged_and_removes_temporary(checkout, note, monkeypatch):
    before = queue_path(checkout).read_bytes()
    monkeypatch.setattr(annotations.os, "replace", Mock(side_effect=OSError(errno.ENOSPC, "full")))
    with pytest.raises(OSError):
        annotations.AnnotationQueue(checkout).append(note)
    assert queue_path(checkout).read_bytes() == before
    assert not list((checkout.parent / "feedback").glob("*.tmp"))


def test_failed_directory_sync_then_retry_confirms_both_file_and_directory(
    checkout, note, monkeypatch
):
    sync = annotations.os.fsync
    calls = []

    def fail_directory(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError(errno.EIO, "disk error")
        sync(descriptor)

    monkeypatch.setattr(annotations.os, "fsync", fail_directory)
    queue = annotations.AnnotationQueue(checkout)
    with pytest.raises(OSError):
        queue.append(note)
    assert "A-001" in queue_path(checkout).read_text()
    before = len(calls)
    assert queue.append(note) == {"id": "A-001", "duplicate": True}
    assert len(calls) - before == 2
    monkeypatch.setattr(annotations.os, "fsync", Mock(side_effect=OSError(errno.EIO, "disk error")))
    with pytest.raises(OSError):
        queue.append(note)


def test_update_changes_only_selected_entry_and_rejects_unknown_id(checkout, note):
    queue = annotations.AnnotationQueue(checkout)
    queue.append(note)
    queue.append(dict(note, request_id=str(uuid4())))
    before = queue_path(checkout).read_text()
    untouched = before[before.index("- [ ] A-002") :]
    queue.update("A-001", "x", Answer="First line.\nSecond line.", Done="Updated source.")
    after = queue_path(checkout).read_text()
    assert after.endswith(untouched)
    assert "  Answer: First line.\n    Second line.\n  Done: Updated source.\n" in after
    with pytest.raises(ValueError):
        queue.update("A-999", "x", Answer="No such note")
    assert queue_path(checkout).read_text() == after


def test_agent_cli_uses_the_same_item_update_operation(checkout, note):
    annotations.AnnotationQueue(checkout).append(note)
    script = checkout / "scripts/annotation_queue.py"
    script.parent.mkdir()
    script.write_text((ROOT / "scripts/annotation_queue.py").read_text())
    answer = checkout / "answer.txt"
    answer.write_text("The database commit survives a restart.\n")
    subprocess.run(
        [sys.executable, str(script), "A-001", "--status", "done", "--answer-file", str(answer)],
        check=True,
        capture_output=True,
        text=True,
    )
    saved = queue_path(checkout).read_text()
    assert "- [x] A-001" in saved
    assert "  Answer: The database commit survives a restart.\n" in saved
    assert annotations.AnnotationQueue(checkout).append(note)["duplicate"]


def test_file_sync_failure_does_not_replace_queue(checkout, note, monkeypatch):
    before = queue_path(checkout).read_bytes()
    monkeypatch.setattr(annotations.os, "fsync", Mock(side_effect=OSError(errno.EIO, "disk error")))
    with pytest.raises(OSError):
        annotations.AnnotationQueue(checkout).append(note)
    assert queue_path(checkout).read_bytes() == before
    assert not list((checkout.parent / "feedback").glob("*.tmp"))


@pytest.fixture
def http_server(checkout, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(helper.subprocess, "run", launch)
    with helper.EditorServer(checkout, None, port=0) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server, launch
        finally:
            server.shutdown()
            thread.join(timeout=2)


def request(server, payload, **overrides):
    from http.client import HTTPConnection

    headers = {
        "Origin": "http://localhost:8080",
        "Content-Type": "application/json",
        "X-Nexo-Editor": "1",
    }
    headers.update(overrides)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST", "/annotations", json.dumps(payload, ensure_ascii=False).encode(), headers
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_annotations_work_without_editor_and_retry_safely(http_server, checkout, note):
    server, launch = http_server
    assert request(server, note) == (201, {"id": "A-001", "duplicate": False})
    assert request(server, note) == (200, {"id": "A-001", "duplicate": True})
    assert request(server, dict(note, note="Changed"))[0] == 409
    assert request(server, dict(note, selected_text=[], targets=[]))[0] == 400
    launch.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://hostile.example"},
        {"Host": "hostile.example"},
        {"X-Nexo-Editor": "0"},
        {"Content-Type": "text/plain"},
        {"Content-Length": "65537"},
    ],
)
def test_http_forgery_and_oversize_cannot_append(http_server, checkout, note, headers):
    server, launch = http_server
    before = queue_path(checkout).read_bytes()
    assert request(server, note, **headers)[0] in {400, 403}
    assert queue_path(checkout).read_bytes() == before
    launch.assert_not_called()


def test_http_disk_error_is_not_reported_as_saved(http_server, checkout, note, monkeypatch):
    server, _ = http_server
    monkeypatch.setattr(
        server.annotations, "replace", Mock(side_effect=PermissionError("read only"))
    )
    status, body = request(server, note)
    assert status == 503 and "retry" in body["error"]
    assert "A-001" not in queue_path(checkout).read_text()


def test_http_accepts_maximum_utf8_arrow_context_without_launching_editor(
    http_server, checkout, drawn_note
):
    server, launch = http_server
    drawn_note["arrows"] = [deepcopy(drawn_note["arrows"][0]) for _ in range(8)]
    for identifier, arrow in enumerate(drawn_note["arrows"], 1):
        arrow["id"] = identifier
        for side in ["from", "to"]:
            arrow[side]["text"] = "📍" * 500
    markers = " ".join(f"[arrow {i}]" for i in range(1, 9))
    drawn_note["note"] = markers + "📍" * (4000 - len(markers))
    drawn_note["selected_text"] = "📍" * 2000
    encoded = json.dumps(drawn_note, ensure_ascii=False).encode()
    assert 32768 < len(encoded) < 65536
    assert request(server, drawn_note) == (201, {"id": "A-001", "duplicate": False})
    assert request(server, drawn_note) == (200, {"id": "A-001", "duplicate": True})
    saved = queue_path(checkout).read_text()
    assert sum(line.startswith("  Arrow ") for line in saved.splitlines()) == 8
    launch.assert_not_called()
