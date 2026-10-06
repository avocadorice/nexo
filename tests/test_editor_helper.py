"""The local helper must not turn web requests into arbitrary file or command access."""

import importlib.util
import json
import subprocess
import sys
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("editor_helper", ROOT / "scripts/editor_helper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "nexo checkout"
    (root / "explorer").mkdir(parents=True)
    (root / "source file.py").write_text("def run():\n    return 1\n")
    (root / ".local").mkdir()
    (root / ".local/secret.env").write_text("PRIVATE=test\n")
    mapping = {
        "components": [{"sources": [{"file": "source file.py"}]}],
        "flows": [{"messages": [{"sources": [{"file": "source file.py"}]}]}],
    }
    (root / "explorer/mapping.json").write_text(json.dumps(mapping))
    return root


@pytest.fixture
def server(source_tree, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(helper.subprocess, "run", launch)
    with helper.EditorServer(source_tree, "/Applications/VS Code/code", port=0) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server, launch
        finally:
            server.shutdown()
            thread.join(timeout=2)


def send(server, *, method="POST", path="/open", body=None, headers=None):
    request_headers = {
        "Origin": "http://localhost:8080",
        "Content-Type": "application/json",
        "X-Nexo-Editor": "1",
    }
    for key, value in (headers or {}).items():
        if value is None:
            request_headers.pop(key, None)
        else:
            request_headers[key] = value
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            method,
            path,
            json.dumps({"file": "source file.py", "line": 2}) if body is None else body,
            request_headers,
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_browser_preflight_and_open_use_exact_file_line_and_safe_argv(server, source_tree):
    http_server, launch = server
    status, headers, _ = send(
        http_server,
        method="OPTIONS",
        headers={
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-nexo-editor",
        },
    )
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:8080"
    assert headers["Access-Control-Allow-Methods"] == "POST"
    assert "Access-Control-Allow-Credentials" not in headers
    launch.assert_not_called()

    status, headers, body = send(http_server, headers={"Origin": "http://127.0.0.1:8080"})
    location = f"{source_tree}/source file.py:2"
    assert status == 200
    assert json.loads(body) == {"opened": location}
    assert headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:8080"
    launch.assert_called_once_with(
        ["/Applications/VS Code/code", "--reuse-window", "--goto", location],
        check=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": None},
        {"Origin": "null"},
        {"Origin": "https://hostile.example"},
        {"Origin": "http://localhost:8080.hostile.example"},
        {"Origin": "http://localhost:8081"},
        {"Host": "hostile.example"},
        {"Host": "localhost:8765"},
        {"X-Nexo-Editor": None},
        {"X-Nexo-Editor": "0"},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Length": "4097"},
        {"Content-Length": "-1"},
        {"Content-Length": "abc"},
        {"Transfer-Encoding": "chunked"},
    ],
)
def test_forged_or_unsafe_http_request_never_launches_editor(server, headers):
    http_server, launch = server
    status, response_headers, _ = send(http_server, headers=headers)
    assert status in {400, 403}
    if headers.get("Origin", "http://localhost:8080") not in helper.ORIGINS:
        assert "Access-Control-Allow-Origin" not in response_headers
    launch.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"method": "GET"},
        {"path": "/open?file=source%20file.py"},
        {"path": "/unknown"},
        {"body": "not json"},
        {"body": "null"},
        {"body": "[]"},
        {"body": '{"file":"source file.py","line":2,"command":"touch /tmp/x"}'},
        {"body": '{"file":".local/secret.env","line":1}'},
        {"body": '{"file":"../outside.py","line":1}'},
        {"body": '{"file":"/etc/passwd","line":1}'},
        {"body": '{"file":"source file.py","line":true}'},
        {"body": '{"file":"source file.py","line":"2"}'},
        {"body": '{"file":"source file.py","line":0}'},
        {"body": '{"file":"source file.py","line":3}'},
    ],
)
def test_invalid_endpoint_or_payload_never_launches_editor(server, options):
    http_server, launch = server
    status, _, _ = send(http_server, **options)
    assert status in {400, 404, 501}
    launch.assert_not_called()


def test_preflight_denies_foreign_origin_or_unexpected_headers(server):
    http_server, launch = server
    for headers in [
        {"Origin": "https://hostile.example"},
        {"Access-Control-Request-Method": "GET"},
        {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-arbitrary-header",
        },
    ]:
        status, _, _ = send(http_server, method="OPTIONS", headers=headers)
        assert status == 403
    launch.assert_not_called()


def test_missing_source_and_symlink_escape_are_rejected_even_if_mapped(server, source_tree):
    http_server, launch = server
    source = source_tree / "source file.py"
    source.unlink()
    assert send(http_server)[0] == 400
    outside = source_tree.parent / "outside.py"
    outside.write_text("private\ndata\n")
    source.symlink_to(outside)
    assert send(http_server)[0] == 400
    launch.assert_not_called()


@pytest.mark.parametrize("name", ["../outside.py", "/etc/passwd", "bad\x00file.py"])
def test_mapping_does_not_override_file_boundary(source_tree, name):
    with pytest.raises(ValueError):
        helper.source_location(source_tree, {name}, {"file": name, "line": 1})


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.CalledProcessError(1, "code"),
        subprocess.TimeoutExpired("code", 10),
    ],
)
def test_editor_failure_is_visible_without_reporting_success(server, error):
    http_server, launch = server
    launch.side_effect = error
    status, _, body = send(http_server)
    assert status == 503
    assert "Copy location" in json.loads(body)["error"]


def test_mac_cli_fallback_does_not_require_global_path_change(tmp_path, monkeypatch):
    cli = tmp_path / "VS Code.app/code"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)
    monkeypatch.setattr(helper, "MAC_CODE", cli)
    monkeypatch.setattr(helper.shutil, "which", lambda _: None)
    assert helper.code_command() == str(cli)
    cli.unlink()
    with pytest.raises(RuntimeError, match="Install VS Code"):
        helper.code_command()
