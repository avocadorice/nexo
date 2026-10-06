"""Open mapped Nexo source in the local VS Code app; run on the Mac, not in Kubernetes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8765
ORIGINS = {"http://localhost:8080", "http://127.0.0.1:8080"}
MAC_CODE = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")


def code_command() -> str:
    command = shutil.which("code")
    if command:
        return command
    if MAC_CODE.is_file() and os.access(MAC_CODE, os.X_OK):
        return str(MAC_CODE)
    raise RuntimeError("Install VS Code and its 'code' shell command before starting this helper.")


def mapped_files(root: Path) -> set[str]:
    mapping = json.loads((root / "explorer/mapping.json").read_text())
    sources = [source for component in mapping["components"] for source in component["sources"]]
    sources += [
        source
        for flow in mapping["flows"]
        for message in flow["messages"]
        for source in message["sources"]
    ]
    return {
        source["file"] for source in sources if source.get("status") != "not implemented"
    }


def source_location(root: Path, allowed: set[str], payload: object) -> str:
    if not isinstance(payload, dict) or set(payload) != {"file", "line"}:
        raise ValueError("Expected a file and line.")
    file, line = payload["file"], payload["line"]
    if not isinstance(file, str) or file not in allowed:
        raise ValueError("Choose a mapped Nexo source file.")
    relative = Path(file)
    if relative.is_absolute() or ".." in relative.parts or "\x00" in file:
        raise ValueError("Source must be inside Nexo.")
    path = (root / relative).resolve(strict=True)
    # Resolve symbolic links before checking the boundary; .local files are not in the mapping.
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Source must be a file inside Nexo.")
    if type(line) is not int or not 1 <= line <= len(path.read_text().splitlines()):
        raise ValueError("Choose a valid source line.")
    return f"{path}:{line}"


class EditorServer(HTTPServer):
    def __init__(self, root: Path, command: str, port: int = PORT):
        self.root = root.resolve()
        self.command = command
        self.allowed = mapped_files(self.root)
        super().__init__(("127.0.0.1", port), EditorHandler)


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args) -> None:
        pass

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Nexo-Editor")
        self.end_headers()
        self.wfile.write(body)

    def trusted_request(self) -> bool:
        # Local binding alone cannot stop a hostile web page from sending a request.
        host = f"127.0.0.1:{self.server.server_port}"
        if (
            self.headers.get_all("Host") != [host]
            or len(self.headers.get_all("Origin", [])) != 1
            or self.headers.get("Origin") not in ORIGINS
        ):
            self.reply(403, {"error": "Use the Nexo explorer at localhost:8080."})
            return False
        if self.path != "/open":
            self.reply(404, {"error": "Unknown helper endpoint."})
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self.trusted_request():
            return
        headers = {
            value.strip().lower()
            for value in self.headers.get("Access-Control-Request-Headers", "").split(",")
        }
        if (
            self.headers.get("Access-Control-Request-Method") != "POST"
            or headers != {"content-type", "x-nexo-editor"}
        ):
            self.reply(403, {"error": "Use the explorer's open button."})
            return
        self.reply(200, {"ready": True})

    def do_POST(self) -> None:
        if not self.trusted_request():
            return
        if (
            self.headers.get_all("X-Nexo-Editor") != ["1"]
            or self.headers.get_content_type() != "application/json"
            or self.headers.get("Transfer-Encoding")
            or len(self.headers.get_all("Content-Length", [])) != 1
        ):
            self.reply(400, {"error": "Use the explorer's JSON request."})
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= 4096:
                raise ValueError("Request is too large or empty.")
            payload = json.loads(self.rfile.read(length))
            location = source_location(self.server.root, self.server.allowed, payload)
        except (ValueError, OSError, RuntimeError):
            self.reply(400, {"error": "Choose an existing mapped Nexo file and a valid line."})
            return
        try:
            subprocess.run(
                [self.server.command, "--reuse-window", "--goto", location],
                check=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            self.reply(503, {"error": "VS Code could not open the file. Use Copy location."})
            return
        self.reply(200, {"opened": location})


def main() -> None:
    with EditorServer(ROOT, code_command()) as server:
        print(f"Nexo editor helper: http://127.0.0.1:{PORT} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
