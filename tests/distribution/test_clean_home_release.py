from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from cortex_platform.product.paths import resolve_paths
from distribution.bundle import BundleBuilder
from distribution.product_manifest import NodeRuntime


_RUNTIME_CORTEXD = r'''
import argparse
import json
import os
import secrets
import signal
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--instance-id", required=True)
parser.add_argument("--config-file", required=True)
parser.add_argument("--config-dir", required=True)
parser.add_argument("--data-dir", required=True)
parser.add_argument("--state-dir", required=True)
parser.add_argument("--cache-dir", required=True)
parser.add_argument("--log-dir", required=True)
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
arguments, _ = parser.parse_known_args()
if arguments.host != "127.0.0.1":
    raise SystemExit(2)
token = secrets.token_urlsafe(32)
stopping = False

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {
                "instance_id": arguments.instance_id,
                "service": "cortexd",
                "status": "ok",
            })
            return
        if self.path == "/api/v1/workspaces":
            if not secrets.compare_digest(
                self.headers.get("X-Cortex-Control-Token", ""), token
            ):
                self.send_json(403, {"category": "control_auth_rejected"})
                return
            self.send_json(200, {
                "items": [{
                    "created_at": "2026-07-28T00:00:00Z",
                    "id": "ws_local",
                    "revision": 0,
                    "title": "Local Research",
                    "updated_at": "2026-07-28T00:00:00Z",
                }],
                "next_cursor": None,
            })
            return
        self.send_error(404)

    def log_message(self, format, *values):
        return

server = ThreadingHTTPServer((arguments.host, arguments.port), Handler)
port = server.server_address[1]
# Normalized exactly as the supervisor and the real daemon normalize it: `ps`
# pads a single-digit day (`Sep  3`), and a raw token fails the comparison on
# the first nine days of every month.
start_token = " ".join(subprocess.check_output([
    "/bin/ps", "-ww", "-p", str(os.getpid()), "-o", "lstart=",
], text=True).split())
state = Path(arguments.state_dir)
state.mkdir(parents=True, exist_ok=True, mode=0o700)
config = Path(arguments.config_dir)
config.mkdir(parents=True, exist_ok=True, mode=0o700)
data = Path(arguments.data_dir)
data.mkdir(parents=True, exist_ok=True, mode=0o700)
Path(arguments.cache_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
Path(arguments.log_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
Path(arguments.config_file).touch(mode=0o600, exist_ok=True)
database = data / "control.db"
if not database.exists():
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            [(version,) for version in range(1, 11)],
        )
        connection.commit()
    finally:
        connection.close()
    # The real ControlStore creates its database and identity companion 0o600
    # and re-validates that on every open; the stand-in must not be laxer.
    database.chmod(0o600)
identity = data / ".control.db.transport.key"
if not identity.exists():
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
metadata = state / "cortexd.json"
temporary = state / f".cortexd.{os.getpid()}"
temporary.write_text(json.dumps({
    "schema_version": 1,
    "pid": os.getpid(),
    "instance_id": arguments.instance_id,
    "start_token": start_token,
    "host": arguments.host,
    "port": port,
    "control_token": token,
}, sort_keys=True))
temporary.chmod(0o600)
temporary.replace(metadata)

def stop(_signum, _frame):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
server.timeout = 0.05
while not stopping:
    server.handle_request()
server.server_close()
try:
    current = json.loads(metadata.read_text())
    if current.get("instance_id") == arguments.instance_id:
        metadata.unlink()
except (FileNotFoundError, ValueError):
    pass
'''


_RUNTIME_NODE = r'''
import http.client
import json
import os
import re
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

host = os.environ["CORTEX_WEB_LISTEN_HOST"]
port = int(os.environ["CORTEX_WEB_LISTEN_PORT"])
build_id = os.environ["CORTEX_WEB_BUILD_ID"]
control_url = os.environ["CORTEX_CONTROL_API_URL"]
control_token = os.environ["CORTEX_CONTROL_TOKEN"]
bootstrap_token = os.environ["CORTEX_ACCESS_BOOTSTRAP_TOKEN"]
if (
    host != "127.0.0.1"
    or re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", control_url) is None
    or re.fullmatch(r"[A-Za-z0-9_-]{43,256}", control_token) is None
    or re.fullmatch(r"[A-Za-z0-9_-]{43}", bootstrap_token) is None
    or os.environ["CORTEX_LOCAL_ACCESS_ENABLED"] != "1"
):
    raise SystemExit(2)
control_port = int(control_url.rsplit(":", 1)[1])
stopping = False

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/_cortex/health":
            self.send_json(200, {
                "adapter_version": 1,
                "build_id": build_id,
                "service": "cortex-web",
                "status": "ok",
            })
            return
        if self.path != "/api/cortex/workspaces":
            self.send_error(404)
            return
        if (
            self.headers.get("Sec-Fetch-Dest") != "empty"
            or self.headers.get("Sec-Fetch-Mode") != "cors"
            or self.headers.get("Sec-Fetch-Site") != "same-origin"
        ):
            self.send_json(403, {"category": "access_boundary_rejected"})
            return
        connection = http.client.HTTPConnection("127.0.0.1", control_port, timeout=1)
        try:
            connection.request("GET", "/api/v1/workspaces", headers={
                "Accept": "application/json",
                "X-Cortex-Control-Token": control_token,
            })
            response = connection.getresponse()
            body = response.read(65537)
        finally:
            connection.close()
        if len(body) > 65536:
            self.send_json(502, {"category": "control_response_invalid"})
            return
        self.send_json(response.status, json.loads(body))

    def log_message(self, format, *values):
        return

server = ThreadingHTTPServer((host, port), Handler)
readiness = {
    "adapter_version": 1,
    "build_id": build_id,
    "event": "ready",
    "host": host,
    "port": server.server_address[1],
    "service": "cortex-web",
}
print(json.dumps(readiness, sort_keys=True), flush=True)

def stop(_signum, _frame):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
server.timeout = 0.05
while not stopping:
    server.handle_request()
server.server_close()
'''


_FAILED_NODE = r'''
import socket
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
time.sleep(0.75)
listener.close()
raise SystemExit(3)
'''


def _runtime_wheels(
    root: Path,
    wheels: tuple[Path, Path],
) -> tuple[Path, Path]:
    root.mkdir()
    output: list[Path] = []
    daemon_module = (
        f"_SOURCE = {_RUNTIME_CORTEXD!r}\n"
        "def main():\n"
        "    namespace = {'__name__': '__main__'}\n"
        "    exec(compile(_SOURCE, '<clean-home-cortexd>', 'exec'), namespace)\n"
    ).encode()
    for wheel in wheels:
        destination = root / wheel.name
        if "cortex_research" in wheel.name:
            shutil.copy2(wheel, destination)
            output.append(destination)
            continue
        with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(destination, "w") as target:
            for member in source.infolist():
                payload = source.read(member.filename)
                if member.filename == "cortex_platform/product/daemon.py":
                    payload = daemon_module
                target.writestr(member, payload)
        output.insert(0, destination)
    return output[0], output[1]


def _bundle(
    root: Path,
    wheels: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    *,
    release_id: str,
    sequence: int,
) -> Path:
    web_root, web_ledger = web_closure
    return BundleBuilder(root).assemble(
        release_id=release_id,
        release_sequence=sequence,
        source_commit=f"{sequence:x}" * 40,
        lock_sha256=f"{sequence + 1:x}" * 64,
        wheels=wheels,
        created_at=f"2026-07-28T13:00:0{sequence}Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256="5" * 64,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path


def _node_stand_in(analyser_node: Path, source: str) -> str:
    """Let a Node stand-in serve the Web closure analyser with a real Node."""

    return (
        "import os\n"
        "import sys\n"
        "if '--no-warnings' in sys.argv[1:]:\n"
        f"    _real = {str(analyser_node)!r}\n"
        "    os.execv(_real, [_real, *sys.argv[1:]])\n"
    ) + source


def _executable(path: Path, source: str) -> Path:
    payload = f"#!{sys.executable}\n{source}".encode()
    if len(payload) < 48 * 1024:
        payload += b"# padding\n" * ((48 * 1024 - len(payload)) // 10 + 1)
    path.write_bytes(payload)
    path.chmod(0o500)
    return path


def _stage_test_node(source: Path, destination: Path, *, stage_root: Path) -> NodeRuntime:
    assert destination.parent == stage_root
    shutil.copyfile(source, destination)
    destination.chmod(0o500)
    return NodeRuntime(
        version="v26.0.0",
        platform="darwin",
        architecture="arm64",
        executable_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
    )


def _closed_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
    }


def _run_cortex(
    prefix: Path,
    home: Path,
    *arguments: str,
    timeout: float = 20,
) -> tuple[int, dict[str, object]]:
    completed = subprocess.run(
        [str(prefix / "bin" / "cortex"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_closed_environment(home),
    )
    payload = json.loads(completed.stdout)
    assert str(prefix.parent.parent.parent) not in json.dumps(payload)
    assert "control_token" not in json.dumps(payload).casefold()
    return completed.returncode, payload


def _request_workspaces(port: int) -> dict[str, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/cortex/workspaces", headers={
            "Accept": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
    assert response.status == 200
    assert isinstance(payload, dict)
    return payload


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path) -> tuple[object, ...]:
    details = path.lstat()
    target = os.readlink(path) if stat.S_ISLNK(details.st_mode) else None
    digest = (
        hashlib.sha256(target.encode()).hexdigest()
        if target is not None
        else _file_hash(path)
    )
    return (
        stat.S_IFMT(details.st_mode),
        stat.S_IMODE(details.st_mode),
        details.st_uid,
        target,
        details.st_size,
        digest,
    )


def _sentinels(home: Path, outside: Path) -> dict[Path, tuple[object, ...]]:
    files = {
        home / "Artifacts" / "paper.md": b"research artifact",
        home / "GDrive" / "result.md": b"gdrive result",
        home / ".hermes" / "state.db": b"hermes state",
        home / "Library" / "LaunchAgents" / "unrelated.plist": b"unrelated service",
        outside: b"outside target",
    }
    for index, (path, contents) in enumerate(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(0o600 | (index % 2) * 0o40)
    link = home / "Artifacts" / "outside-link"
    link.symlink_to(outside)
    return {path: _snapshot(path) for path in (*files, link)}


def _assert_sentinels(expected: dict[Path, tuple[object, ...]]) -> None:
    assert {path: _snapshot(path) for path in expected} == expected


def _claim_is_alive(claim: dict[str, object]) -> bool:
    completed = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(claim["pid"]), "-o", "lstart="],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    # The claim's token is normalized (by the supervisor for lifecycle.json,
    # by the daemon double above for cortexd.json); compare like for like, or
    # this liveness check goes vacuous on days 1-9 of the month.
    return completed.returncode == 0 and " ".join(completed.stdout.split()) == claim["start_token"]


def _assert_port_rebinds(port: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            listening = probe.connect_ex(("127.0.0.1", port)) == 0
        if not listening:
            try:
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("127.0.0.1", port))
                return
            except OSError:
                pass
        time.sleep(0.02)
    raise AssertionError(f"port {port} was not released")


def _install(
    prefix: Path,
    bundle: Path,
    node: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, object]:
    from distribution.cli import main

    assert main([
        "install",
        "--bundle",
        str(bundle),
        "--prefix",
        str(prefix),
        "--node-executable",
        str(node),
        "--allow-unsigned-developer",
    ]) == 0
    return json.loads(capsys.readouterr().out)["result"]


def _upgrade(
    prefix: Path,
    bundle: Path,
    runtime_root: Path,
    home: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    from distribution.cli import main

    result = main([
        "upgrade",
        "--bundle",
        str(bundle),
        "--prefix",
        str(prefix),
        "--runtime-root",
        str(runtime_root),
        "--home",
        str(home),
        "--allow-unsigned-developer",
    ])
    return result, json.loads(capsys.readouterr().out)


def test_clean_home_fresh_state_release_lifecycle(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.install as install

    monkeypatch.setattr(install, "stage_node_runtime", _stage_test_node)
    home = tmp_path / "clean home"
    home.mkdir(mode=0o700)
    paths = resolve_paths(environ={"HOME": str(home)}, platform="darwin")
    prefix = paths.data_dir.parent / "Distribution"
    sentinels = _sentinels(home, tmp_path / "outside.txt")
    wheels = _runtime_wheels(tmp_path / "runtime-wheels", wheel_pair)
    first = _bundle(
        tmp_path / "bundle-one",
        wheels,
        web_closure,
        analyser_node,
        release_id="cortex-dev-1",
        sequence=1,
    )
    second = _bundle(
        tmp_path / "bundle-two",
        wheels,
        web_closure,
        analyser_node,
        release_id="cortex-dev-2",
        sequence=2,
    )
    node = _executable(tmp_path / "node", _node_stand_in(analyser_node, _RUNTIME_NODE))

    installed = _install(prefix, first, node, capsys)
    assert not paths.control_database_file.exists()
    upgraded_code, upgraded = _upgrade(
        prefix,
        second,
        paths.runtime_update_root,
        home,
        capsys,
    )
    assert upgraded_code == 0
    assert upgraded["result"]["action"] == "upgraded"
    _assert_sentinels(sentinels)

    code, started = _run_cortex(prefix, home, "start", "--home", str(home), "--timeout", "8")
    assert code == 0
    assert started["result"]["state"] == "running"
    web_port = started["result"]["web_listener"]["port"]
    assert _request_workspaces(web_port)["items"][0]["id"] == "ws_local"
    code, doctor = _run_cortex(
        prefix,
        home,
        "doctor",
        "--runtime-root",
        str(paths.runtime_update_root),
        "--home",
        str(home),
    )
    assert code == 0
    assert doctor["result"]["developer_usable"] is True
    assert doctor["result"]["ga_ready"] is False
    record = json.loads((paths.runtime_update_root / "lifecycle.json").read_text())
    claims = [record["supervisor"], *record["children"].values()]
    ports = [record["children"][role]["port"] for role in ("control", "web")]
    state_hashes = {
        paths.control_database_file: _file_hash(paths.control_database_file),
        paths.control_database_file.with_name(
            ".control.db.transport.key"
        ): _file_hash(
            paths.control_database_file.with_name(".control.db.transport.key")
        ),
    }

    code, stopped = _run_cortex(prefix, home, "stop", "--home", str(home), "--timeout", "5")
    assert code == 0
    assert stopped["result"] == {"state": "stopped"}
    assert not any(_claim_is_alive(claim) for claim in claims)
    for port in ports:
        _assert_port_rebinds(port)

    code, rolled_back = _run_cortex(
        prefix,
        home,
        "rollback",
        "--runtime-root",
        str(paths.runtime_update_root),
        "--home",
        str(home),
    )
    assert code == 0
    assert rolled_back["result"]["release_id"] == "cortex-dev-1"
    assert installed["version"] == rolled_back["result"]["version"]
    code, restarted = _run_cortex(prefix, home, "start", "--home", str(home), "--timeout", "8")
    assert code == 0
    assert restarted["result"]["state"] == "running"
    assert _request_workspaces(restarted["result"]["web_listener"]["port"])["items"]
    assert _run_cortex(prefix, home, "stop", "--home", str(home), "--timeout", "5")[0] == 0
    assert {path: _file_hash(path) for path in state_hashes} == state_hashes
    _assert_sentinels(sentinels)

    code, uninstalled = _run_cortex(
        prefix,
        home,
        "uninstall",
        "--runtime-root",
        str(paths.runtime_update_root),
        "--home",
        str(home),
    )
    assert code == 0
    assert uninstalled["result"] == {"uninstalled": True}
    assert not prefix.exists()
    assert {path: _file_hash(path) for path in state_hashes} == state_hashes
    _assert_sentinels(sentinels)


def test_clean_home_existing_state_upgrade_fails_closed(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.install as install

    monkeypatch.setattr(install, "stage_node_runtime", _stage_test_node)
    home = tmp_path / "clean home"
    home.mkdir(mode=0o700)
    paths = resolve_paths(environ={"HOME": str(home)}, platform="darwin")
    prefix = paths.data_dir.parent / "Distribution"
    sentinels = _sentinels(home, tmp_path / "outside.txt")
    wheels = _runtime_wheels(tmp_path / "runtime-wheels", wheel_pair)
    first = _bundle(
        tmp_path / "bundle-one",
        wheels,
        web_closure,
        analyser_node,
        release_id="cortex-dev-1",
        sequence=1,
    )
    second = _bundle(
        tmp_path / "bundle-two",
        wheels,
        web_closure,
        analyser_node,
        release_id="cortex-dev-2",
        sequence=2,
    )
    node = _executable(tmp_path / "node", _node_stand_in(analyser_node, _RUNTIME_NODE))
    installed = _install(prefix, first, node, capsys)

    code, started = _run_cortex(prefix, home, "start", "--home", str(home), "--timeout", "8")
    assert code == 0
    assert _request_workspaces(started["result"]["web_listener"]["port"])["items"]
    assert _run_cortex(prefix, home, "stop", "--home", str(home), "--timeout", "5")[0] == 0
    state_hashes = {
        paths.control_database_file: _file_hash(paths.control_database_file),
        paths.control_database_file.with_name(
            ".control.db.transport.key"
        ): _file_hash(
            paths.control_database_file.with_name(".control.db.transport.key")
        ),
    }
    current_before = (prefix / "current.json").read_bytes()
    lkg_before = (prefix / "last-known-good.json").read_bytes()
    versions_before = {path.name for path in (prefix / "versions").iterdir()}

    result, failed = _upgrade(
        prefix,
        second,
        paths.runtime_update_root,
        home,
        capsys,
    )
    assert result == 1
    assert failed["error"] == "verified backup authorization is required"
    assert (prefix / "current.json").read_bytes() == current_before
    assert (prefix / "last-known-good.json").read_bytes() == lkg_before
    assert {path.name for path in (prefix / "versions").iterdir()} == versions_before
    assert not (prefix / "snapshots").exists()
    assert {path: _file_hash(path) for path in state_hashes} == state_hashes
    _assert_sentinels(sentinels)

    code, _payload = _run_cortex(
        prefix,
        home,
        "uninstall",
        "--runtime-root",
        str(paths.runtime_update_root),
        "--home",
        str(home),
    )
    assert code == 0
    assert installed["release_id"] == "cortex-dev-1"
    assert {path: _file_hash(path) for path in state_hashes} == state_hashes
    _assert_sentinels(sentinels)


def test_clean_home_failed_start_and_stop_leave_no_owned_process_or_listener(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.install as install

    monkeypatch.setattr(install, "stage_node_runtime", _stage_test_node)
    home = tmp_path / "clean home"
    home.mkdir(mode=0o700)
    paths = resolve_paths(environ={"HOME": str(home)}, platform="darwin")
    prefix = paths.data_dir.parent / "Distribution"
    wheels = _runtime_wheels(tmp_path / "runtime-wheels", wheel_pair)
    bundle = _bundle(
        tmp_path / "bundle",
        wheels,
        web_closure,
        analyser_node,
        release_id="cortex-dev-1",
        sequence=1,
    )
    node = _executable(
        tmp_path / "failed-node", _node_stand_in(analyser_node, _FAILED_NODE)
    )
    _install(prefix, bundle, node, capsys)

    process = subprocess.Popen(
        [
            str(prefix / "bin" / "cortex"),
            "start",
            "--home",
            str(home),
            "--timeout",
            "3",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_closed_environment(home),
    )
    metadata: dict[str, object] | None = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and process.poll() is None:
        try:
            metadata = json.loads(paths.daemon_metadata_file.read_text())
            break
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    stdout, stderr = process.communicate(timeout=8)
    assert process.returncode == 1, stdout + stderr
    assert metadata is not None
    control_claim = {
        "pid": metadata["pid"],
        "start_token": metadata["start_token"],
    }
    assert not _claim_is_alive(control_claim)
    _assert_port_rebinds(metadata["port"])
    assert not (paths.runtime_update_root / "lifecycle.json").exists()
    assert not (paths.runtime_update_root / "shutdown.sock").exists()
    assert not paths.daemon_metadata_file.exists()
