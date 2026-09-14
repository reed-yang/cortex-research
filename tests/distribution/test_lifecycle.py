from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import plistlib
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from distribution.bundle import BundleBuilder
from distribution.product_manifest import PRODUCT_MANIFEST_SCHEMA  # noqa: F401
from distribution.product_manifest import NodeRuntime, PythonRuntime, build_product_manifest
from distribution.schema import canonical_json_bytes

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _preserve_legacy_lifecycle_layout_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.name.startswith("test_canonical_product_paths"):
        return

    import distribution.lifecycle as lifecycle
    from distribution.product_paths import InstalledProductPaths

    selected_runtime: list[Path | None] = [None]
    original_init = lifecycle.LifecycleManager.__init__
    original_render = lifecycle.render_launch_agent
    original_resolver = lifecycle.resolve_installed_product_paths
    original_stage = lifecycle.stage_launch_agent

    def resolve_paths(*args: object, **kwargs: object) -> InstalledProductPaths:
        root = selected_runtime[0]
        if root is None:
            return original_resolver(*args, **kwargs)
        return InstalledProductPaths(
            config_file=root / "config" / "config.toml",
            config_dir=root / "config",
            data_dir=root / "data",
            state_dir=root / "control-state",
            cache_dir=root / "cache",
            log_dir=root / "logs",
            control_database_file=root / "data" / "control.db",
            runtime_update_root=root,
        )

    def initialize(
        manager: lifecycle.LifecycleManager,
        generation: Path,
        runtime_root: Path | None = None,
        *,
        home: Path,
        environment: dict[str, str] | None = None,
        pin_tools: bool = True,
    ) -> None:
        selected_runtime[0] = runtime_root
        original_init(
            manager,
            generation,
            runtime_root,
            home=home,
            environment=environment,
            pin_tools=pin_tools,
        )

    def render(
        generation: Path,
        runtime_root: Path,
        **kwargs: object,
    ) -> bytes:
        selected_runtime[0] = runtime_root
        return original_render(generation, runtime_root, **kwargs)

    def stage(
        generation: Path,
        runtime_root: Path,
        staging_root: Path,
        **kwargs: object,
    ) -> lifecycle.ServiceDefinition:
        selected_runtime[0] = runtime_root
        return original_stage(generation, runtime_root, staging_root, **kwargs)

    monkeypatch.setattr(lifecycle, "resolve_installed_product_paths", resolve_paths)
    monkeypatch.setattr(lifecycle.LifecycleManager, "__init__", initialize)
    monkeypatch.setattr(lifecycle, "render_launch_agent", render)
    monkeypatch.setattr(lifecycle, "stage_launch_agent", stage)


def test_lifecycle_surface_is_exported_from_distribution() -> None:
    import distribution

    assert distribution.LifecycleManager.__name__ == "LifecycleManager"
    assert distribution.LifecycleError.__name__ == "LifecycleError"


def _executable(path: Path, source: str, *, minimum_size: int = 0) -> None:
    payload = f"#!{sys.executable}\n{source}".encode("utf-8")
    if len(payload) < minimum_size:
        payload += b"# padding\n" * ((minimum_size - len(payload)) // 10 + 1)
    path.write_bytes(payload)
    path.chmod(0o500)


def _node_stand_in(analyser_node: Path, source: str | None = None) -> str:
    """Wrap a Node stand-in so the Web closure analyser reaches a real Node.

    Verifying an installed generation parses its payload with the generation's
    own staged Node. These stand-ins are Python doubles for the web server, and
    they run under a minimal environment with no usable PATH, so the real Node
    path is baked in at creation time.
    """

    return (
        "import os\n"
        "import sys\n"
        "if '--no-warnings' in sys.argv[1:]:\n"
        f"    _real = {str(analyser_node)!r}\n"
        "    os.execv(_real, [_real, *sys.argv[1:]])\n"
    ) + (_FAKE_NODE if source is None else source)


def _port_is_closed(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _json_get(
    port: int,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", path, headers=headers or {"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    payload = json.loads(body)
    assert isinstance(payload, dict)
    return response.status, payload


_FAKE_CORTEXD = r'''
import argparse
import json
import os
import secrets
import signal
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
            candidate = self.headers.get("X-Cortex-Control-Token", "")
            if not secrets.compare_digest(candidate, token):
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
(data / "control.db").write_bytes(b"SQLite format 3\\0test")
(data / ".control.db.transport.key").write_bytes(b"i" * 32)
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


_FAKE_NODE = r'''
import http.client
import json
import os
import re
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AUTHORITY_CAPTURE = None

host = os.environ["CORTEX_WEB_LISTEN_HOST"]
port = int(os.environ["CORTEX_WEB_LISTEN_PORT"])
build_id = os.environ["CORTEX_WEB_BUILD_ID"]
control_url = os.environ["CORTEX_CONTROL_API_URL"]
control_token = os.environ["CORTEX_CONTROL_TOKEN"]
bootstrap_token = os.environ["CORTEX_ACCESS_BOOTSTRAP_TOKEN"]
local_enabled = os.environ["CORTEX_LOCAL_ACCESS_ENABLED"]
if (
    host != "127.0.0.1"
    or re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", control_url) is None
    or int(control_url.rsplit(":", 1)[1]) > 65535
    or re.fullmatch(r"[A-Za-z0-9_-]{43,256}", control_token) is None
    or re.fullmatch(r"[A-Za-z0-9_-]{43}", bootstrap_token) is None
    or local_enabled != "1"
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
        authority = f"{host}:{server.server_address[1]}"
        local_proof = {
            "bootstrap": bootstrap_token,
            "forwarded_for": host,
            "forwarded_host": authority,
            "forwarded_port": str(server.server_address[1]),
            "forwarded_proto": "http",
            "host": authority,
        }
        if local_proof != {
            "bootstrap": bootstrap_token,
            "forwarded_for": "127.0.0.1",
            "forwarded_host": authority,
            "forwarded_port": str(server.server_address[1]),
            "forwarded_proto": "http",
            "host": authority,
        }:
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
        except (OSError, http.client.HTTPException):
            self.send_json(502, {"category": "control_unavailable"})
            return
        finally:
            connection.close()
        if len(body) > 65536:
            self.send_json(502, {"category": "control_response_invalid"})
            return
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            self.send_json(502, {"category": "control_response_invalid"})
            return
        self.send_json(response.status, payload)

    def log_message(self, format, *values):
        return

server = ThreadingHTTPServer((host, port), Handler)
port = server.server_address[1]
readiness = {
    "adapter_version": 1,
    "build_id": build_id,
    "event": "ready",
    "host": host,
    "port": port,
    "service": "cortex-web",
}
if AUTHORITY_CAPTURE is not None:
    capture = Path(AUTHORITY_CAPTURE)
    with capture.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": sys.argv,
            "bootstrap_token": bootstrap_token,
            "control_token": control_token,
            "control_url": control_url,
            "local_enabled": local_enabled,
            "readiness": readiness,
        }, sort_keys=True) + "\n")
    capture.chmod(0o600)
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


def _installed_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    *,
    embedded_python: bool = False,
    venv_interpreter: bytes | None = None,
) -> Path:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-3",
        release_sequence=3,
        source_commit="3" * 40,
        lock_sha256="4" * 64,
        wheels=wheel_pair,
        created_at="2026-07-27T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256="5" * 64,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    generation = tmp_path / "versions" / "cortex-dev-3-generation"
    generation.mkdir(parents=True, mode=0o700)
    shutil.copytree(bundle, generation / "bundle")
    shutil.copytree(bundle / "artifacts" / "web", generation / "web")

    cortexd = generation / "runtime" / "bin" / "cortexd"
    cortexd.parent.mkdir(parents=True)
    _executable(cortexd, _FAKE_CORTEXD)
    installed_site = generation / "runtime" / "installed-site"
    installed_paths = installed_site / "cortex_platform" / "product" / "paths.py"
    installed_paths.parent.mkdir(parents=True)
    (installed_site / "cortex_platform" / "__init__.py").write_text("")
    (installed_paths.parent / "__init__.py").write_text("")
    installed_paths.write_text(
        (REPOSITORY / "cortex_platform" / "product" / "paths.py").read_text()
    )
    python = generation / "runtime" / "bin" / "python"
    _executable(
        python,
        (
            "import os, sys\n"
            "from pathlib import Path\n"
            "installed_site = str(Path(__file__).resolve().parents[2] / 'runtime' / 'installed-site')\n"
            "bootstrap = f'import sys;sys.path.insert(0,{installed_site!r});'\n"
            "arguments = sys.argv[1:]\n"
            "if '-c' in arguments:\n"
            "    index = arguments.index('-c') + 1\n"
            "    arguments[index] = bootstrap + arguments[index]\n"
            "os.execv(sys.executable, [sys.executable, *arguments])\n"
        ),
    )

    node = generation / "node-runtime" / "bin" / "node"
    node.parent.mkdir(parents=True)
    _executable(node, _node_stand_in(analyser_node), minimum_size=48 * 1024)
    runtime = NodeRuntime(
        version="v26.0.0",
        platform="darwin",
        architecture="arm64",
        executable_sha256=hashlib.sha256(node.read_bytes()).hexdigest(),
    )
    embedded = None
    if embedded_python:
        # The generation's venv interpreter is a copy of the staged one, which
        # is what `--copies` against the embedded runtime actually produces.
        interpreter = generation / "python-runtime" / "bin" / "python3.14"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(python.read_bytes())
        interpreter.chmod(0o500)
        if venv_interpreter is not None:
            original = stat.S_IMODE(python.stat().st_mode)
            python.chmod(0o700)
            python.write_bytes(venv_interpreter)
            python.chmod(original)
        embedded = PythonRuntime(
            version="3.14.6",
            platform="darwin",
            architecture="arm64",
            interpreter_sha256=hashlib.sha256(interpreter.read_bytes()).hexdigest(),
            archive_sha256="7" * 64,
            venv_interpreter_sha256=hashlib.sha256(python.read_bytes()).hexdigest(),
        )
    (generation / "product-manifest.json").write_bytes(
        canonical_json_bytes(build_product_manifest(runtime, embedded)) + b"\n"
    )
    return generation


def _installed_distribution_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> tuple[Path, Path, dict[str, object]]:
    from distribution.bundle import verify_bundle

    source = _installed_generation(tmp_path / "generation-source", wheel_pair, web_closure, analyser_node)
    bundle = verify_bundle(source / "bundle", node_executable=analyser_node)
    version = f"{bundle.manifest['release_id']}-{bundle.digest[:16]}"
    prefix = tmp_path / "distribution"
    generation = prefix / "versions" / version
    generation.parent.mkdir(parents=True)
    source.rename(generation)
    pointer = {
        "schema_version": 1,
        "version": version,
        "release_id": bundle.manifest["release_id"],
        "release_sequence": bundle.manifest["release_sequence"],
        "bundle_digest": bundle.digest,
    }
    (prefix / "current.json").write_text(json.dumps(pointer, sort_keys=True) + "\n")
    launchers = prefix / "bin"
    launchers.mkdir()
    _executable(launchers / "cortex", "raise SystemExit(0)\n")
    _executable(launchers / "cortexd", "raise SystemExit(0)\n")
    return prefix, generation, pointer


def test_canonical_product_paths_own_all_lifecycle_product_state(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from cortex_platform.product.paths import resolve_paths

    from distribution.lifecycle import LifecycleManager, render_launch_agent

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    home = tmp_path / "home"
    paths = resolve_paths(environ={"HOME": str(home)}, platform="darwin")
    service = plistlib.loads(
        render_launch_agent(
            generation,
            paths.runtime_update_root,
            home=home,
            environment={},
        )
    )
    service_arguments = service["ProgramArguments"]
    assert service_arguments[service_arguments.index("--config-file") + 1] == str(
        paths.config_file
    )
    assert service_arguments[service_arguments.index("--config-dir") + 1] == str(
        paths.config_dir
    )
    assert service_arguments[service_arguments.index("--data-dir") + 1] == str(
        paths.data_dir
    )
    assert service_arguments[service_arguments.index("--state-dir") + 1] == str(
        paths.state_dir
    )
    assert service_arguments[service_arguments.index("--cache-dir") + 1] == str(
        paths.cache_dir
    )
    assert service_arguments[service_arguments.index("--log-dir") + 1] == str(
        paths.log_dir
    )
    assert service["StandardErrorPath"] == str(paths.log_dir / "service-supervisor.log")
    assert service["StandardOutPath"] == str(paths.log_dir / "service-supervisor.log")
    manager = LifecycleManager(
        generation,
        home=home,
        environment={},
    )
    try:
        manager.start(timeout=8)
        assert manager.runtime_root == paths.runtime_update_root
        assert paths.config_file.is_file()
        assert paths.config_dir.is_dir()
        assert paths.control_database_file.is_file()
        assert paths.control_database_file.with_name(
            ".control.db.transport.key"
        ).is_file()
        assert paths.daemon_metadata_file.is_file()
        assert paths.cache_dir.is_dir()
        assert (paths.log_dir / "cortexd.log").is_file()
        assert (paths.log_dir / "web.log").is_file()
        assert (paths.log_dir / "supervisor.log").is_file()
        assert not (paths.runtime_update_root / "data").exists()
        assert not (paths.runtime_update_root / "control-state").exists()
    finally:
        manager.stop(timeout=5)


def test_canonical_product_paths_reject_alternate_runtime_before_child_start(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    alternate = tmp_path / "alternate-runtime"

    with pytest.raises(lifecycle.LifecycleError, match="canonical lifecycle root"):
        lifecycle.LifecycleManager(
            generation,
            alternate,
            home=tmp_path / "home",
            environment={},
        )
    assert not alternate.exists()
    assert not (tmp_path / "home" / "Library" / "Logs" / "Cortex").exists()


def test_canonical_product_paths_service_bootstrap_passes_exact_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle
    from distribution.product_paths import InstalledProductPaths

    generation = tmp_path / "generation"
    home = tmp_path / "home"
    state = home / "state"
    paths = InstalledProductPaths(
        config_file=home / "config" / "config.toml",
        config_dir=home / "config",
        data_dir=home / "data",
        state_dir=state,
        cache_dir=home / "cache",
        log_dir=home / "logs",
        control_database_file=home / "data" / "control.db",
        runtime_update_root=state / "runtime-update",
    )
    executed: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "resolve_installed_product_paths",
        lambda *_args, **_kwargs: paths,
    )

    def execv(_executable: str, command: list[str]) -> None:
        executed.extend(command)
        raise RuntimeError("exec captured")

    monkeypatch.setattr(lifecycle.os, "execv", execv)
    with pytest.raises(RuntimeError, match="exec captured"):
        lifecycle._service_entry(
            [
                str(tmp_path / "module-root"),
                "--generation",
                str(generation),
                "--runtime-root",
                str(paths.runtime_update_root),
                "--home",
                str(home),
                "--expected-generation",
                "g" * 64,
                "--startup-timeout",
                "10.0",
            ]
        )

    assert executed[executed.index("--config-file") + 1] == str(paths.config_file)
    assert executed[executed.index("--config-dir") + 1] == str(paths.config_dir)
    assert executed[executed.index("--data-dir") + 1] == str(paths.data_dir)
    assert executed[executed.index("--state-dir") + 1] == str(paths.state_dir)
    assert executed[executed.index("--cache-dir") + 1] == str(paths.cache_dir)
    assert executed[executed.index("--log-dir") + 1] == str(paths.log_dir)


def test_doctor_requires_running_exact_generation_and_browser_probe(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from distribution.install import DistributionInstaller
    from distribution.lifecycle import LifecycleManager

    prefix, generation, pointer = _installed_distribution_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
    )
    monkeypatch.setattr(DistributionInstaller, "_health_check", lambda *_args: None)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    home = tmp_path / "home"
    installer = DistributionInstaller(prefix)

    stopped = installer.doctor(runtime_root=runtime_root, home=home)
    assert stopped["installed"] is True
    assert stopped["developer_usable"] is False
    assert stopped["web_url"] is None
    assert stopped["category"] == "stopped"

    manager = LifecycleManager(generation, runtime_root, home=home)
    try:
        running = manager.start(timeout=8)
        report = installer.doctor(runtime_root=runtime_root, home=home)
        assert report["installed"] is True
        assert report["release_id"] == pointer["release_id"]
        assert report["version_digest"] == pointer["bundle_digest"]
        assert report["developer_usable"] is True
        assert report["web_url"] == f"http://127.0.0.1:{running.web_port}"
        assert report["category"] == "healthy"
        rendered = json.dumps(report, sort_keys=True)
        assert str(REPOSITORY) not in rendered
        assert str(generation) not in rendered
        assert "token" not in rendered.casefold()
    finally:
        manager.stop(timeout=5)


def test_doctor_rejects_runtime_owned_by_another_installed_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from distribution.install import DistributionInstaller
    from distribution.lifecycle import LifecycleManager

    prefix, _installed_generation_path, _pointer = _installed_distribution_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
    )
    running_generation = _installed_generation(
        tmp_path / "running-source",
        wheel_pair,
        web_closure,
        analyser_node,
    )
    running_manifest_path = running_generation / "product-manifest.json"
    running_manifest = json.loads(running_manifest_path.read_text())
    running_manifest["node_runtime"]["version"] = "v24.0.0"
    running_manifest_path.write_bytes(canonical_json_bytes(running_manifest) + b"\n")
    monkeypatch.setattr(DistributionInstaller, "_health_check", lambda *_args: None)
    runtime_root = tmp_path / "runtime"
    home = tmp_path / "home"
    running = LifecycleManager(running_generation, runtime_root, home=home)
    try:
        running.start(timeout=8)

        report = DistributionInstaller(prefix).doctor(runtime_root=runtime_root, home=home)

        assert report["developer_usable"] is False
        assert report["web_url"] is None
        assert report["category"] == "wrong_generation"
    finally:
        running.stop(timeout=5)


def test_distribution_cli_doctor_is_read_only_and_reports_sanitized_json(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from distribution.cli import main
    from distribution.install import DistributionInstaller

    prefix, _generation, _pointer = _installed_distribution_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
    )
    monkeypatch.setattr(DistributionInstaller, "_health_check", lambda *_args: None)
    runtime_root = tmp_path / "missing-runtime"

    assert main(
        [
            "doctor",
            "--prefix",
            str(prefix),
            "--runtime-root",
            str(runtime_root),
            "--home",
            str(tmp_path / "home"),
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["installed"] is True
    assert output["result"]["developer_usable"] is False
    assert output["result"]["category"] == "runtime_root_missing"
    assert not runtime_root.exists()
    rendered = json.dumps(output, sort_keys=True)
    assert str(REPOSITORY) not in rendered
    assert "token" not in rendered.casefold()


def test_canonical_product_paths_cli_doctor_passes_only_path_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.cli as cli

    observed: dict[str, object] = {}

    class RecordingInstaller:
        def __init__(self, prefix: Path | None) -> None:
            observed["prefix"] = prefix

        def doctor(self, **kwargs: object) -> dict[str, object]:
            observed.update(kwargs)
            return {"developer_usable": False}

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SECRET_TOKEN", "must-not-cross-boundary")
    monkeypatch.setattr(cli, "DistributionInstaller", RecordingInstaller)

    assert cli.main(
        [
            "doctor",
            "--prefix",
            str(tmp_path / "distribution"),
            "--home",
            str(tmp_path / "home"),
        ]
    ) == 0
    capsys.readouterr()
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["CORTEX_DATA_DIR"] == str(tmp_path / "data")
    assert environment["CORTEX_STATE_DIR"] == str(tmp_path / "state")
    assert "SECRET_TOKEN" not in environment


def test_generation_contract_binds_verified_payload_and_minimal_child_environments(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import load_generation

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    layout = load_generation(generation)

    assert layout.root == generation
    assert len(layout.identity) == 64
    assert layout.release_build_id == "cortex-r0-build-1"
    assert layout.private_access_enabled is False
    assert layout.control_environment(home=tmp_path / "home") == {
        "HOME": str(tmp_path / "home"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(generation / "runtime" / "bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
    }
    assert layout.web_environment(
        control_port=8799,
        control_token="C" * 43,
        bootstrap_token="B" * 43,
    ) == {
        "CORTEX_ACCESS_BOOTSTRAP_TOKEN": "B" * 43,
        "CORTEX_CONTROL_API_URL": "http://127.0.0.1:8799",
        "CORTEX_CONTROL_TOKEN": "C" * 43,
        "CORTEX_LOCAL_ACCESS_ENABLED": "1",
        "CORTEX_WEB_BUILD_ID": "cortex-r0-build-1",
        "CORTEX_WEB_LISTEN_HOST": "127.0.0.1",
        "CORTEX_WEB_LISTEN_PORT": "0",
        "HOME": "",
        "LANG": "C",
        "LC_ALL": "C",
        "NODE_OPTIONS": "",
        "NODE_PATH": "",
        "PATH": str(generation / "node-runtime" / "bin"),
    }


def test_control_ready_repr_omits_control_token() -> None:
    import distribution.lifecycle as lifecycle

    marker_token = "control-authority-marker"
    ready = lifecycle.ControlReady(
        claim=lifecycle.ControlClaim(
            pid=1234,
            start_token="start",
            claim="claim",
            host="127.0.0.1",
            port=8765,
            instance_id="instance",
        ),
        control_token=marker_token,
    )

    assert marker_token not in repr(ready)


def test_generation_contract_rejects_web_copy_and_node_identity_tampering(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, load_generation

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    web_entry = generation / "web" / "server" / "index.js"
    web_entry.write_bytes(web_entry.read_bytes() + b"tampered")
    with pytest.raises(LifecycleError, match="Web payload copy"):
        load_generation(generation)

    generation = _installed_generation(
        tmp_path / "node-case",
        wheel_pair,
        web_closure,
        analyser_node,
    )
    manifest_path = generation / "product-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["node_runtime"]["executable_sha256"] = "0" * 64
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    with pytest.raises(LifecycleError, match="Node runtime digest"):
        load_generation(generation)


def test_local_front_door_owns_generation_children_and_refreshes_daemon_token(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    authority_capture = tmp_path / "web-authority.jsonl"
    node = generation / "node-runtime" / "bin" / "node"
    node.chmod(0o700)
    _executable(
        node,
        _node_stand_in(
            analyser_node,
            _FAKE_NODE.replace(
                "AUTHORITY_CAPTURE = None",
                f"AUTHORITY_CAPTURE = {str(authority_capture)!r}",
                1,
            ),
        ),
        minimum_size=48 * 1024,
    )
    manifest_path = generation / "product-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["node_runtime"]["executable_sha256"] = hashlib.sha256(node.read_bytes()).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    runtime_root = tmp_path / "lifecycle"
    manager = lifecycle.LifecycleManager(
        generation,
        runtime_root,
        home=tmp_path / "home",
    )
    ports: set[int] = set()
    try:
        first = manager.start(timeout=8)
        ports.update((first.control_port, first.web_port))
        assert first.state == "running"
        assert first.generation_identity == manager.generation.identity
        assert first.web_url == f"http://127.0.0.1:{first.web_port}"
        assert manager.status().state == "running"

        state = json.loads((runtime_root / "lifecycle.json").read_text())
        assert state["generation_identity"] == manager.generation.identity
        assert set(state["children"]) == {"control", "web"}
        assert "control_token" not in json.dumps(state)
        assert manager.generation.identity[:16] in state["children"]["control"]["claim"]
        first_token = json.loads(
            (runtime_root / "control-state" / "cortexd.json").read_text()
        )["control_token"]
        authority_runs = [
            json.loads(line)
            for line in authority_capture.read_text(encoding="utf-8").splitlines()
        ]
        assert len(authority_runs) == 1
        first_authority = authority_runs[0]
        first_bootstrap = first_authority["bootstrap_token"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first_bootstrap) is not None
        assert first_authority["control_url"] == f"http://127.0.0.1:{first.control_port}"
        if not secrets.compare_digest(first_authority["control_token"], first_token):
            pytest.fail("Web did not receive the exact in-memory Control authority")
        assert first_authority["local_enabled"] == "1"

        browser_headers = {
            "Accept": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        browser_status, browser_payload = _json_get(
            first.web_port,
            "/api/cortex/workspaces",
            headers=browser_headers,
        )
        assert browser_status == 200
        assert browser_payload == {
            "items": [{
                "created_at": "2026-07-28T00:00:00Z",
                "id": "ws_local",
                "revision": 0,
                "title": "Local Research",
                "updated_at": "2026-07-28T00:00:00Z",
            }],
            "next_cursor": None,
        }

        first_public_output = json.dumps(state) + json.dumps(browser_payload)
        first_public_output += json.dumps(first_authority["argv"])
        first_public_output += json.dumps(first_authority["readiness"])
        first_public_output += json.dumps(
            manager._supervisor_command(run_id="0" * 32, claim="0" * 64, timeout=8)
        )
        first_public_output += "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (runtime_root / "logs").iterdir()
            if path.is_file()
        )
        for authority in (first_token, first_bootstrap):
            if authority in first_public_output:
                pytest.fail("runtime authority leaked through public or durable output")

        assert manager.stop(timeout=5).state == "stopped"
        assert manager.status().state == "stopped"
        second = manager.start(timeout=8)
        ports.update((second.control_port, second.web_port))
        second_token = json.loads(
            (runtime_root / "control-state" / "cortexd.json").read_text()
        )["control_token"]
        second_state = json.loads((runtime_root / "lifecycle.json").read_text())
        authority_runs = [
            json.loads(line)
            for line in authority_capture.read_text(encoding="utf-8").splitlines()
        ]
        assert len(authority_runs) == 2
        second_authority = authority_runs[1]
        second_bootstrap = second_authority["bootstrap_token"]
        if secrets.compare_digest(second_token, first_token):
            pytest.fail("Cortexd authority did not rotate across supervisor starts")
        if secrets.compare_digest(second_bootstrap, first_bootstrap):
            pytest.fail("Web bootstrap authority did not rotate across supervisor starts")
        rejected_status, _ = _json_get(
            second.control_port,
            "/api/v1/workspaces",
            headers={"Accept": "application/json", "X-Cortex-Control-Token": first_token},
        )
        assert rejected_status == 403
        assert _json_get(
            second.web_port,
            "/api/cortex/workspaces",
            headers=browser_headers,
        )[0] == 200

        second_public_output = json.dumps(second_state)
        second_public_output += json.dumps(second_authority["argv"])
        second_public_output += json.dumps(second_authority["readiness"])
        second_public_output += "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (runtime_root / "logs").iterdir()
            if path.is_file()
        )
        for authority in (first_token, first_bootstrap, second_token, second_bootstrap):
            if authority in second_public_output:
                pytest.fail("runtime authority leaked through public or durable output")
    finally:
        manager.stop(timeout=5)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(_port_is_closed(port) for port in ports):
            break
        time.sleep(0.02)
    assert all(_port_is_closed(port) for port in ports)


def test_front_door_probe_validates_claims_before_calling_only_web(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "lifecycle",
        home=tmp_path / "home",
    )
    assert manager.probe_local_front_door() == lifecycle.LocalFrontDoorHealth(
        healthy=False,
        web_url=None,
        category="stopped",
    )
    running = manager.start(timeout=8)
    assert running.control_port is not None
    assert running.web_port is not None
    browser_headers = {
        "Accept": "application/json",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    calls: list[tuple[int, str, dict[str, str] | None]] = []
    real_json_request = lifecycle._json_request

    def observed_json_request(
        port: int,
        path: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        deadline: float | None = None,
    ) -> tuple[int, dict[str, object]] | None:
        calls.append((port, path, headers))
        return real_json_request(
            port,
            path,
            timeout=timeout,
            headers=headers,
            deadline=deadline,
        )

    try:
        with monkeypatch.context() as context:
            context.setattr(lifecycle, "_json_request", observed_json_request)
            health = manager.probe_local_front_door()
        assert health == lifecycle.LocalFrontDoorHealth(
            healthy=True,
            web_url=f"http://127.0.0.1:{running.web_port}",
            category="healthy",
        )
        assert calls == [
            (running.web_port, "/api/cortex/workspaces", browser_headers),
        ]

        with monkeypatch.context() as context:
            context.setattr(
                lifecycle,
                "_listener_owned",
                lambda _pid, _port, **_kwargs: False,
            )
            context.setattr(
                lifecycle,
                "_json_request",
                lambda *_args, **_kwargs: pytest.fail(
                    "front-door probe performed HTTP before validating claims"
                ),
            )
            rejected = manager.probe_local_front_door()
        assert rejected == lifecycle.LocalFrontDoorHealth(
            healthy=False,
            web_url=None,
            category="claims_unhealthy",
        )
    finally:
        manager.stop(timeout=5)


def test_front_door_probe_rejects_generation_changed_during_web_request(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "lifecycle",
        home=tmp_path / "home",
    )
    record = lifecycle.LifecycleRecord(
        generation_identity=manager.generation.identity,
        generation_root=str(manager.generation.root),
        run_id="0" * 32,
        supervisor=lifecycle.ProcessClaim(1234, "start", "s" * 64),
        control=lifecycle.ControlClaim(
            1235,
            "start",
            "c" * 64,
            "127.0.0.1",
            8765,
            "c" * 64,
        ),
        web=lifecycle.WebClaim(
            1236,
            "start",
            "w" * 64,
            "127.0.0.1",
            8766,
            manager.generation.release_build_id,
        ),
    )
    # A generation swap is an activation, and an activation rewrites the
    # lifecycle record -- that record is what says which generation this
    # runtime belongs to. The probe verifies the generation ONCE (it is a
    # bundle re-hash plus the pinned Web closure analysis, the probe's dominant
    # cost) and re-reads the record afterwards, so the swap is staged where a
    # real one happens rather than by handing `_current_generation` a different
    # object on its second call.
    swapped = replace(
        record,
        generation_identity="f" * 64,
        generation_root=str(tmp_path / "replacement-generation"),
    )
    records = [record]
    observed_deadlines: list[float] = []

    def valid_identity(*_args: object, deadline: float) -> bool:
        observed_deadlines.append(deadline)
        return True

    def valid_listener(*_args: object, deadline: float) -> bool:
        observed_deadlines.append(deadline)
        return True

    def change_generation(
        *_args: object,
        timeout: float,
        deadline: float,
        **_kwargs: object,
    ) -> tuple[int, dict[str, object]]:
        assert timeout > 0
        observed_deadlines.append(deadline)
        records[0] = swapped
        return 200, {"items": [], "next_cursor": None}

    monkeypatch.setattr(lifecycle, "_read_record", lambda _path: records[0])
    monkeypatch.setattr(manager, "_current_generation", lambda: manager.generation)
    monkeypatch.setattr(lifecycle, "_identity_matches", valid_identity)
    monkeypatch.setattr(lifecycle, "_listener_owned", valid_listener)
    monkeypatch.setattr(lifecycle, "_json_request", change_generation)

    assert manager.probe_local_front_door(timeout=0.5) == lifecycle.LocalFrontDoorHealth(
        healthy=False,
        web_url=None,
        category="front_door_unhealthy",
    )
    assert len(set(observed_deadlines)) == 1


def test_stop_preserves_authority_when_the_supervisor_cannot_acknowledge_shutdown(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = LifecycleManager(
        generation,
        tmp_path / "lifecycle",
        home=tmp_path / "home",
    )
    running = manager.start(timeout=8)
    assert running.supervisor_pid is not None
    supervisor_pid = running.supervisor_pid
    try:
        os.kill(supervisor_pid, signal.SIGSTOP)
        with pytest.raises(LifecycleError, match="shutdown"):
            manager.stop(timeout=0.1)
        assert (tmp_path / "lifecycle" / "lifecycle.json").exists()
        assert (tmp_path / "lifecycle" / "control-state" / "cortexd.json").exists()
        os.kill(supervisor_pid, 0)
    finally:
        try:
            os.kill(supervisor_pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        manager.stop(timeout=5)


def test_launch_agent_is_staged_privately_without_activation_or_runtime_secrets(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, render_launch_agent, stage_launch_agent

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    home = tmp_path / "home"
    staging = home / "Library" / "Application Support" / "Cortex" / "Service Plans"
    rendered = render_launch_agent(
        generation,
        tmp_path / "runtime",
        home=home,
    )
    assert not (tmp_path / "runtime").exists()
    result = stage_launch_agent(
        generation,
        tmp_path / "runtime",
        staging,
        home=home,
    )

    assert result.path == staging / "ai.cortex.devrel.supervisor.plist"
    assert result.path.stat().st_mode & 0o777 == 0o600
    assert result.launchctl_invoked is False
    assert not (home / "Library" / "LaunchAgents").exists()
    payload = result.path.read_bytes()
    assert payload == rendered
    definition = plistlib.loads(payload)
    assert definition["Label"] == "ai.cortex.devrel.supervisor"
    assert definition["RunAtLoad"] is False
    assert definition["KeepAlive"] is False
    assert definition["WorkingDirectory"] == str(generation)
    assert definition["ProgramArguments"][0] == str(generation / "runtime" / "bin" / "python")
    assert "-B" in definition["ProgramArguments"]
    assert str(generation / "bundle" / "tools") in definition["ProgramArguments"]
    serialized = payload.decode("utf-8").casefold()
    assert "launchctl" not in serialized
    assert "token" not in serialized
    assert "--claim" not in serialized
    assert "--run-id" not in serialized
    assert str(REPOSITORY).casefold() not in serialized

    repeated = stage_launch_agent(
        generation,
        tmp_path / "runtime",
        staging,
        home=home,
    )
    assert repeated.sha256 == result.sha256
    assert repeated.path.read_bytes() == payload

    with pytest.raises(LifecycleError, match="LaunchAgents"):
        stage_launch_agent(
            generation,
            tmp_path / "runtime",
            home / "Library" / "LaunchAgents",
            home=home,
        )


def test_distribution_cli_exposes_start_status_and_stop_without_doctor_claims(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from distribution.cli import main

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    common = [
        "--generation",
        str(generation),
        "--runtime-root",
        str(runtime),
        "--home",
        str(tmp_path / "home"),
    ]
    try:
        assert main(["status", *common]) == 0
        stopped = json.loads(capsys.readouterr().out)
        assert stopped["result"] == {"state": "stopped"}

        assert main(["start", *common, "--timeout", "8"]) == 0
        started = json.loads(capsys.readouterr().out)
        assert started["result"]["state"] == "running"
        assert started["result"]["generation_identity"]
        assert "developer_usable" not in json.dumps(started)

        assert main(["status", *common]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["result"]["state"] == "running"

        assert main(["stop", *common, "--timeout", "5"]) == 0
        stopped = json.loads(capsys.readouterr().out)
        assert stopped["result"] == {"state": "stopped"}
    finally:
        from distribution.lifecycle import LifecycleManager

        LifecycleManager(
            generation,
            runtime,
            home=tmp_path / "home",
        ).stop(timeout=2)


@pytest.mark.parametrize("command", ["start", "status", "stop"])
def test_current_generation_cli_resolves_lifecycle_from_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    import distribution.cli as cli
    from distribution.lifecycle import LifecycleStatus

    prefix = tmp_path / "distribution"
    generation = prefix / "versions" / "current-generation"
    observed: dict[str, object] = {}

    class RecordingInstaller:
        def __init__(self, selected_prefix: Path | None) -> None:
            observed["prefix"] = selected_prefix

        @contextmanager
        def current_generation_binding(self):
            yield generation

    class RecordingLifecycle:
        def __init__(
            self,
            selected_generation: Path,
            runtime_root: Path | None,
            *,
            home: Path,
            environment: dict[str, str],
        ) -> None:
            observed.update(
                generation=selected_generation,
                runtime_root=runtime_root,
                home=home,
                environment=environment,
            )

        def start(self, *, timeout: float) -> LifecycleStatus:
            observed["timeout"] = timeout
            return LifecycleStatus("stopped")

        def status(self) -> LifecycleStatus:
            return LifecycleStatus("stopped")

        def stop(self, *, timeout: float) -> LifecycleStatus:
            observed["timeout"] = timeout
            return LifecycleStatus("stopped")

    monkeypatch.setattr(cli, "DistributionInstaller", RecordingInstaller)
    monkeypatch.setattr(cli, "LifecycleManager", RecordingLifecycle)

    assert cli.main(
        [
            command,
            "--prefix",
            str(prefix),
            "--home",
            str(tmp_path / "home"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"state": "stopped"}
    assert observed["prefix"] == prefix
    assert observed["generation"] == generation
    assert observed["runtime_root"] is None


def test_current_generation_cli_rejects_a_generation_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from distribution.cli import main

    assert main(
        [
            "status",
            "--prefix",
            str(tmp_path / "distribution"),
            "--generation",
            str(tmp_path / "other-generation"),
        ]
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "installed lifecycle cannot override the current generation",
        "ok": False,
    }


def test_current_generation_cli_holds_pointer_binding_through_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import distribution.cli as cli
    from distribution.lifecycle import LifecycleStatus

    prefix = tmp_path / "distribution"
    generation = prefix / "versions" / "current-generation"
    binding_active = False

    class BindingInstaller:
        def __init__(self, _selected_prefix: Path | None) -> None:
            pass

        def current_generation(self) -> Path:
            raise AssertionError("unbound current generation read")

        @contextmanager
        def current_generation_binding(self):
            nonlocal binding_active
            binding_active = True
            try:
                yield generation
            finally:
                binding_active = False

    class BindingLifecycle:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            assert binding_active

        def start(self, *, timeout: float) -> LifecycleStatus:
            assert binding_active
            assert timeout == 10
            return LifecycleStatus("stopped")

    monkeypatch.setattr(cli, "DistributionInstaller", BindingInstaller)
    monkeypatch.setattr(cli, "LifecycleManager", BindingLifecycle)

    assert cli.main(["start", "--prefix", str(prefix)]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"state": "stopped"}
    assert binding_active is False


def test_stop_rejects_forged_broad_child_claims_without_signaling(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager, _current_start_token

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    start_token = _current_start_token(os.getpid())
    assert start_token is not None
    forged = {
        "schema_version": 1,
        "generation_identity": manager.generation.identity,
        "generation_root": str(generation),
        "run_id": "1" * 32,
        "supervisor": {
            "pid": os.getpid(),
            "start_token": start_token,
            "claim": "f" * 64,
        },
        "children": {
            "control": {
                "pid": os.getpid(),
                "start_token": start_token,
                "claim": "/",
                "host": "127.0.0.1",
                "port": 1,
                "instance_id": "/",
            },
            "web": {
                "pid": os.getpid(),
                "start_token": start_token,
                "claim": "/",
                "host": "127.0.0.1",
                "port": 1,
                "build_id": "forged",
            },
        },
    }
    state = runtime / "lifecycle.json"
    state.write_bytes(canonical_json_bytes(forged) + b"\n")
    state.chmod(0o600)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, signum: signals.append((pid, signum)))

    with pytest.raises(LifecycleError, match="claim"):
        manager.stop(timeout=0)
    assert signals == []


def test_quiescence_lease_blocks_a_concurrent_start(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    failures: list[BaseException] = []

    def start() -> None:
        try:
            manager.start(timeout=0.1)
        except BaseException as exc:
            failures.append(exc)

    with manager.quiescence(timeout=1):
        thread = threading.Thread(target=start)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert len(failures) == 1
    assert isinstance(failures[0], LifecycleError)
    assert "lifecycle lock timed out" in str(failures[0])


def test_quiescence_rejects_a_running_lifecycle(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    try:
        manager.start(timeout=8)
        with pytest.raises(LifecycleError, match="requires the lifecycle to be stopped"):
            with manager.quiescence(timeout=1):
                pytest.fail("running lifecycle obtained a quiescence lease")
    finally:
        manager.stop(timeout=5)


def test_foreground_supervisor_holds_an_exclusive_lifetime_lock(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import fcntl

    from distribution.lifecycle import LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    try:
        manager.start(timeout=8)
        descriptor = os.open(runtime / ".supervisor.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
    finally:
        manager.stop(timeout=5)


def test_status_revalidates_generation_bytes_after_start(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    adapter = generation / "web" / "server" / "node-adapter.mjs"
    original = adapter.read_bytes()
    try:
        manager.start(timeout=8)
        adapter.write_bytes(original + b"tampered")
        with pytest.raises(LifecycleError, match="Web payload copy"):
            manager.status()
    finally:
        adapter.write_bytes(original)
        manager.stop(timeout=5)


def test_service_staging_rejects_symlink_ancestors_without_target_residue(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, stage_launch_agent

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "staging-alias"
    alias.symlink_to(launch_agents, target_is_directory=True)

    with pytest.raises(LifecycleError, match="symlink ancestor"):
        stage_launch_agent(
            generation,
            tmp_path / "runtime",
            alias / "plans",
            home=home,
        )
    assert not (launch_agents / "plans").exists()


def test_service_staging_never_follows_a_directory_swap_into_launch_agents(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, mode=0o700)
    staging = tmp_path / "staging"
    moved = tmp_path / "staging-moved"
    original_open = lifecycle.os.open
    swapped = False

    def swap_after_directory_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if (
            not swapped
            and Path(os.fspath(path)) == staging
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            staging.rename(moved)
            staging.symlink_to(launch_agents, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(lifecycle.os, "open", swap_after_directory_open)
    with pytest.raises(lifecycle.LifecycleError, match="changed while writing"):
        lifecycle.stage_launch_agent(
            generation,
            tmp_path / "runtime",
            staging,
            home=home,
        )

    assert swapped is True
    assert list(launch_agents.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_service_staging_removes_the_definition_when_directory_fsync_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    staging = tmp_path / "staging"
    original_fsync = lifecycle.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(lifecycle.os, "fsync", fail_directory_fsync)
    with pytest.raises(lifecycle.LifecycleError, match="staged safely"):
        lifecycle.stage_launch_agent(
            generation,
            tmp_path / "runtime",
            staging,
            home=tmp_path / "home",
        )

    assert list(staging.iterdir()) == []


def test_generation_rejects_a_writable_process_path_component(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, load_generation

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    server = generation / "web" / "server"
    server.chmod(0o777)
    try:
        with pytest.raises(LifecycleError, match="generation path is unsafe"):
            load_generation(generation)
    finally:
        server.chmod(0o755)


def test_lifecycle_root_rejects_symlink_ancestors_without_target_residue(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(external, target_is_directory=True)

    with pytest.raises(LifecycleError, match="symlink ancestor"):
        LifecycleManager(
            generation,
            alias / "runtime",
            home=tmp_path / "home",
        )
    assert not (external / "runtime").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system anchors only")
@pytest.mark.parametrize(
    ("alias_parent", "canonical_parent"),
    [
        (Path("/var/tmp"), Path("/private/var/tmp")),
        (Path("/tmp"), Path("/private/tmp")),
    ],
    ids=("var", "tmp"),
)
def test_lifecycle_root_accepts_trusted_macos_system_symlink_anchor(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    alias_parent: Path,
    canonical_parent: Path,
) -> None:
    from distribution.lifecycle import LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    canonical_root = Path(
        tempfile.mkdtemp(prefix="cortex-lifecycle-", dir=canonical_parent)
    )
    alias_root = alias_parent / canonical_root.name
    try:
        manager = LifecycleManager(
            generation,
            alias_root,
            home=tmp_path / "home",
        )

        assert manager.runtime_root == canonical_root
    finally:
        shutil.rmtree(canonical_root)


def test_service_staging_accepts_the_installer_managed_venv_python_symlink(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import stage_launch_agent

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    python = generation / "runtime" / "bin" / "python"
    python.unlink()
    python.symlink_to(Path(sys.executable))

    result = stage_launch_agent(
        generation,
        tmp_path / "runtime",
        tmp_path / "staging",
        home=tmp_path / "home",
    )
    definition = plistlib.loads(result.path.read_bytes())
    assert definition["ProgramArguments"][0] == str(python)


def test_concurrent_starts_converge_on_one_claimed_supervisor(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    barrier = threading.Barrier(2)
    results = []
    failures: list[BaseException] = []

    def start() -> None:
        try:
            manager = LifecycleManager(
                generation,
                runtime,
                home=tmp_path / "home",
            )
            barrier.wait(timeout=2)
            results.append(manager.start(timeout=8))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=12)
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert len(results) == 2
        assert {result.supervisor_pid for result in results} == {
            results[0].supervisor_pid
        }
        assert {result.control_port for result in results} == {results[0].control_port}
        assert {result.web_port for result in results} == {results[0].web_port}
    finally:
        manager.stop(timeout=5)


def test_failed_web_start_cleans_the_control_child_and_lifetime_claim(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import fcntl

    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    node = generation / "node-runtime" / "bin" / "node"
    node.chmod(0o700)
    _executable(
        node,
        _node_stand_in(analyser_node, "raise SystemExit(9)\n"),
        minimum_size=48 * 1024,
    )
    manifest_path = generation / "product-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["node_runtime"]["executable_sha256"] = hashlib.sha256(node.read_bytes()).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )

    with pytest.raises(LifecycleError, match="supervisor exited|readiness"):
        manager.start(timeout=2)
    assert not (runtime / "lifecycle.json").exists()
    assert not (runtime / "control-state" / "cortexd.json").exists()
    descriptor = os.open(runtime / ".supervisor.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_runtime_state_rejects_boolean_schema_version_confusion(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    state_path = runtime / "lifecycle.json"
    state: dict[str, object] | None = None
    try:
        manager.start(timeout=8)
        state = json.loads(state_path.read_text())
        state["schema_version"] = True
        state_path.write_bytes(canonical_json_bytes(state) + b"\n")
        state_path.chmod(0o600)
        with pytest.raises(LifecycleError, match="schema"):
            manager.status()
    finally:
        if state_path.exists() and state is not None:
            state["schema_version"] = 1
            state_path.write_bytes(canonical_json_bytes(state) + b"\n")
            state_path.chmod(0o600)
        manager.stop(timeout=5)


def test_start_and_stop_refuse_a_runtime_owned_by_another_generation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, LifecycleManager

    first_generation = _installed_generation(tmp_path / "first", wheel_pair, web_closure, analyser_node)
    second_generation = _installed_generation(tmp_path / "second", wheel_pair, web_closure, analyser_node)
    second_manifest_path = second_generation / "product-manifest.json"
    second_manifest = json.loads(second_manifest_path.read_text())
    second_manifest["node_runtime"]["version"] = "v24.0.0"
    second_manifest_path.write_bytes(canonical_json_bytes(second_manifest) + b"\n")
    runtime = tmp_path / "runtime"
    first = LifecycleManager(
        first_generation,
        runtime,
        home=tmp_path / "home",
    )
    second = LifecycleManager(
        second_generation,
        runtime,
        home=tmp_path / "home",
    )
    assert first.generation.identity != second.generation.identity
    try:
        first.start(timeout=8)
        with pytest.raises(LifecycleError, match="another generation"):
            second.stop(timeout=1)
        with pytest.raises(LifecycleError, match="another generation"):
            second.start(timeout=1)
        assert first.status().state == "running"
    finally:
        first.stop(timeout=5)


def test_default_supervisor_uses_the_installed_generation_python_and_modules(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    monkeypatch.setattr(lifecycle.sys, "executable", str(tmp_path / "missing-python"))

    try:
        assert manager.start(timeout=8).state == "running"
    finally:
        manager.stop(timeout=5)


def test_startup_timeout_cleans_children_metadata_listeners_and_lifetime_lock(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import fcntl

    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    marker = tmp_path / "web-bound.json"
    cortexd = generation / "runtime" / "bin" / "cortexd"
    cortexd.chmod(0o700)
    _executable(
        cortexd,
        _FAKE_CORTEXD.replace(
            "signal.signal(signal.SIGTERM, stop)",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        ),
    )
    node = generation / "node-runtime" / "bin" / "node"
    node.chmod(0o700)
    source = _FAKE_NODE.replace(
        "print(json.dumps(readiness",
        f"open({str(marker)!r}, 'w').write(json.dumps({{'pid': os.getpid(), 'port': port}}))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "import time\ntime.sleep(30)\nprint(json.dumps(readiness",
        1,
    )
    _executable(node, _node_stand_in(analyser_node, source), minimum_size=48 * 1024)
    manifest_path = generation / "product-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["node_runtime"]["executable_sha256"] = hashlib.sha256(node.read_bytes()).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )

    with pytest.raises(LifecycleError, match="readiness timed out"):
        manager.start(timeout=2)

    bound = json.loads(marker.read_text())
    assert _port_is_closed(bound["port"])
    with pytest.raises(ProcessLookupError):
        os.kill(bound["pid"], 0)
    assert not (runtime / "control-state" / "cortexd.json").exists()
    descriptor = os.open(runtime / ".supervisor.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_provisional_direct_child_is_reaped_before_a_start_token_is_captured() -> None:
    import distribution.lifecycle as lifecycle

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "provisional-claim"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        lifecycle._terminate_spawned(
            process,
            start_token=None,
            claim="provisional-claim",
            timeout=0.2,
        )
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def test_spawned_child_cleanup_uses_its_direct_handle_after_identity_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "known-claim"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    monkeypatch.setattr(
        lifecycle,
        "_terminate_owned",
        lambda *_args, **_kwargs: pytest.fail("direct child cleanup used a bare PID path"),
    )
    try:
        lifecycle._terminate_spawned(
            process,
            start_token="known-start-token",
            claim="known-claim",
            timeout=0.5,
        )
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def test_normal_stop_uses_the_supervisor_authenticated_shutdown_channel(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = lifecycle.LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    try:
        manager.start(timeout=8)
        channel = lifecycle._shutdown_socket_path(runtime)
        details = channel.lstat()
        assert stat.S_ISSOCK(details.st_mode)
        assert stat.S_IMODE(details.st_mode) == 0o600
        with monkeypatch.context() as patch:
            patch.setattr(
                lifecycle,
                "_signal_owned",
                lambda *_args, **_kwargs: pytest.fail(
                    "normal stop attempted a bare PID signal"
                ),
            )
            assert manager.stop(timeout=5).state == "stopped"
        assert not channel.exists()
    finally:
        manager.stop(timeout=5)


def test_a_long_stop_timeout_is_acknowledged_and_completes_promptly(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = lifecycle.LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    try:
        manager.start(timeout=8)
        channel = lifecycle._shutdown_socket_path(runtime)
        assert channel.exists()
        with monkeypatch.context() as patch:
            patch.setattr(
                lifecycle,
                "_signal_owned",
                lambda *_args, **_kwargs: pytest.fail(
                    "a long stop timeout attempted a bare PID signal"
                ),
            )
            started = time.monotonic()
            assert manager.stop(timeout=120).state == "stopped"
            # A generous timeout is a bound, never a wait: the supervisor
            # acknowledges the request and exits at its own pace.
            assert time.monotonic() - started < 20
        assert not channel.exists()
        assert not (runtime / "lifecycle.json").exists()
    finally:
        manager.stop(timeout=5)


def test_a_long_stop_timeout_fits_the_installed_supervisor_deadline_bound(
    tmp_path: Path,
) -> None:
    """A long caller deadline is sent as a budget the installed server accepts.

    The supervisor that is already installed refuses any request whose deadline
    is more than sixty seconds ahead, so the stand-in below repeats that exact
    rule against a real socket, a real claimed process and the real client.
    """

    import distribution.lifecycle as lifecycle

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    claim = "b" * 64
    supervisor = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)", claim],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener, path, identity = lifecycle._open_shutdown_listener(runtime)
    requests: list[dict[str, object]] = []
    refused: list[object] = []

    def installed_supervisor() -> None:
        listener.settimeout(10)
        try:
            connection, _address = listener.accept()
        except OSError:
            return
        with connection:
            try:
                raw = lifecycle._read_shutdown_message(
                    connection,
                    deadline=time.monotonic() + 0.5,
                )
            except (lifecycle.LifecycleError, OSError):
                return
            assert isinstance(raw, dict)
            requests.append(raw)
            if not time.monotonic() < raw["deadline"] <= time.monotonic() + 60:
                refused.append(raw["deadline"])
                return
            connection.sendall(
                canonical_json_bytes(
                    {"schema_version": 1, "accepted": True, "claim": claim}
                )
                + b"\n"
            )
        supervisor.terminate()

    server = threading.Thread(target=installed_supervisor)
    server.start()
    try:
        start_token = lifecycle._current_start_token(supervisor.pid)
        assert start_token is not None
        record = lifecycle.LifecycleRecord(
            generation_identity="c" * 64,
            generation_root=str(tmp_path / "generation"),
            run_id="1" * 32,
            supervisor=lifecycle.ProcessClaim(supervisor.pid, start_token, claim),
            control=lifecycle.ControlClaim(
                supervisor.pid, start_token, "control-claim", "127.0.0.1", 1111, "token"
            ),
            web=lifecycle.WebClaim(
                supervisor.pid, start_token, "web-claim", "127.0.0.1", 2222, "build"
            ),
        )
        started = time.monotonic()
        lifecycle._request_supervisor_shutdown(runtime, record, deadline=started + 120)
        assert time.monotonic() - started < 20
        assert refused == []
        budget = float(requests[0]["deadline"]) - started
        assert 60 <= budget <= 65
    finally:
        server.join(timeout=15)
        listener.close()
        lifecycle._remove_shutdown_socket(path, identity)
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.wait(timeout=5)


def test_start_reaps_supervisor_when_its_start_token_cannot_be_captured(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = lifecycle.subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        # Verifying the generation runs the Web closure analyser through
        # subprocess.run, which spawns its own Popen. Count lifecycle children.
        if "--no-warnings" not in (list(args[0]) if args else []):
            spawned.append(process)
        return process

    def fail_start_token(*_args: object, **_kwargs: object) -> str:
        raise lifecycle.LifecycleError(
            "child process identity was not established"
        )

    monkeypatch.setattr(lifecycle.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(lifecycle, "_wait_for_start_token", fail_start_token)

    try:
        with pytest.raises(lifecycle.LifecycleError, match="identity was not established"):
            manager.start(timeout=30)

        assert len(spawned) == 1
        spawned[0].wait(timeout=2)
        assert spawned[0].poll() is not None
    finally:
        for process in spawned:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


def test_start_reaps_supervisor_when_closing_its_log_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    spawned: list[subprocess.Popen[bytes]] = []
    real_open_log = lifecycle._open_log
    real_popen = lifecycle.subprocess.Popen

    class FailingCloseLog:
        def __init__(self, path: Path) -> None:
            self.handle = real_open_log(path)

        def fileno(self) -> int:
            return self.handle.fileno()

        def close(self) -> None:
            self.handle.close()
            raise OSError("injected supervisor log close failure")

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        # Verifying the generation runs the Web closure analyser through
        # subprocess.run, which spawns its own Popen. Count lifecycle children.
        if "--no-warnings" not in (list(args[0]) if args else []):
            spawned.append(process)
        return process

    monkeypatch.setattr(lifecycle, "_open_log", FailingCloseLog)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", recording_popen)

    try:
        with pytest.raises(OSError, match="injected supervisor log close failure"):
            manager.start(timeout=30)

        assert len(spawned) == 1
        spawned[0].wait(timeout=2)
        assert spawned[0].poll() is not None
    finally:
        for process in spawned:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


def test_failed_start_keeps_authority_when_supervisor_termination_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    removals: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_terminate_spawned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            lifecycle.LifecycleError("injected termination failure")
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_remove_matching_control_metadata",
        lambda *_args, **_kwargs: removals.append("metadata"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_remove_matching_record",
        lambda *_args, **_kwargs: removals.append("state"),
    )

    with pytest.raises(lifecycle.LifecycleError, match="termination failure"):
        manager._cleanup_failed_start(
            object(),
            start_token=None,
            claim="f" * 64,
            deadline=time.monotonic() + 1,
        )
    assert removals == []


def test_term_then_kill_respects_one_total_cleanup_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    deadlines: list[float] = []
    ticks = iter((10.0, 10.2))
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: next(ticks, 10.2))
    monkeypatch.setattr(lifecycle, "_signal_owned", lambda *_args, **_kwargs: True)

    def wait_for_exit(
        _process: lifecycle.ProcessClaim,
        *,
        deadline: float,
    ) -> bool:
        deadlines.append(deadline)
        return False

    monkeypatch.setattr(lifecycle, "_wait_identity_exit", wait_for_exit)

    with pytest.raises(lifecycle.LifecycleError, match="cleanup bound"):
        lifecycle._terminate_owned(
            lifecycle.ProcessClaim(1234, "start", "bounded-claim"),
            timeout=0.2,
        )
    assert deadlines == [pytest.approx(10.1), pytest.approx(10.2)]


def test_lifecycle_rejects_non_finite_timeouts_before_spawning(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("non-finite timeout spawned a process"),
    )

    for timeout in (math.nan, math.inf, -math.inf):
        with pytest.raises(lifecycle.LifecycleError, match="timeout"):
            manager.start(timeout=timeout)
        with pytest.raises(lifecycle.LifecycleError, match="timeout"):
            manager.stop(timeout=timeout)


def test_signal_owned_rechecks_identity_immediately_before_signaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    process = lifecycle.ProcessClaim(1234, "start", "owned-claim")
    observations = iter(
        [
            ("start", "command owned-claim"),
            ("replacement", "unrelated command"),
        ]
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        lifecycle,
        "_process_details",
        lambda _pid, **_kwargs: next(observations),
    )
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, signum: signals.append((pid, signum)))

    assert lifecycle._signal_owned(process, signal.SIGTERM) is False
    assert signals == []


def test_linux_signaling_uses_a_pidfd_bound_before_identity_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    process = lifecycle.ProcessClaim(1234, "start", "owned-claim")
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    monkeypatch.setattr(
        lifecycle.os,
        "pidfd_open",
        lambda pid, flags: calls.append(("open", pid, flags)) or 91,
        raising=False,
    )
    monkeypatch.setattr(
        lifecycle.signal,
        "pidfd_send_signal",
        lambda descriptor, signum, siginfo, flags: calls.append(
            ("signal", descriptor, signum, siginfo, flags)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        lifecycle,
        "_process_details",
        lambda pid, **_kwargs: calls.append(("inspect", pid))
        or ("start", "command owned-claim"),
    )
    monkeypatch.setattr(lifecycle.os, "close", lambda descriptor: calls.append(("close", descriptor)))
    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda *_args: pytest.fail("Linux signaling fell back to a bare PID"),
    )

    assert lifecycle._signal_owned(process, signal.SIGTERM) is True
    assert calls[0] == ("open", 1234, 0)
    assert calls[-2:] == [("signal", 91, signal.SIGTERM, None, 0), ("close", 91)]


def test_web_health_rejects_a_listener_not_owned_by_the_claimed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    web = lifecycle.WebClaim(
        1234,
        "start",
        "web-claim",
        "127.0.0.1",
        43210,
        "build",
    )
    monkeypatch.setattr(
        lifecycle,
        "_json_request",
        lambda *_args, **_kwargs: (
            200,
            {
                "adapter_version": 1,
                "build_id": "build",
                "service": "cortex-web",
                "status": "ok",
            },
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_listener_owned",
        lambda _pid, _port: False,
        raising=False,
    )

    assert lifecycle._web_healthy(web) is False


def test_stop_preserves_authority_when_authenticated_shutdown_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "runtime",
        home=tmp_path / "home",
    )
    record = lifecycle.LifecycleRecord(
        generation_identity=manager.generation.identity,
        generation_root=str(generation),
        run_id="1" * 32,
        supervisor=lifecycle.ProcessClaim(1, "supervisor", "s" * 64),
        control=lifecycle.ControlClaim(
            2, "control", "control-claim", "127.0.0.1", 1111, "control-claim"
        ),
        web=lifecycle.WebClaim(
            3, "web", "web-claim", "127.0.0.1", 2222, "build"
        ),
    )
    removals: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_request_supervisor_shutdown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            lifecycle.LifecycleError("authenticated shutdown failed")
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_identity_matches",
        lambda _process, **_kwargs: True,
    )
    monkeypatch.setattr(
        lifecycle,
        "_remove_matching_control_metadata",
        lambda *_args, **_kwargs: removals.append("metadata"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_remove_matching_record",
        lambda *_args, **_kwargs: removals.append("state"),
    )

    with pytest.raises(lifecycle.LifecycleError, match="authenticated shutdown failed"):
        manager._stop_record(record, deadline=time.monotonic() + 1)
    assert removals == []


def test_process_observation_distinguishes_absence_from_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")

    def completed(
        returncode: int, stdout: str, stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(1, ""),
    )
    assert lifecycle._process_details(999_999_999) is None

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(1, "", "ps: process id too large\n"),
    )
    with pytest.raises(lifecycle.LifecycleError, match="observation failed"):
        lifecycle._process_details(999_999_999)

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(0, "malformed"),
    )
    with pytest.raises(lifecycle.LifecycleError, match="observation failed"):
        lifecycle._process_details(1234)


def test_stop_preserves_authority_when_process_observation_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = lifecycle.LifecycleManager(
        generation,
        runtime,
        home=tmp_path / "home",
    )
    state_path = runtime / "lifecycle.json"
    metadata_path = runtime / "control-state" / "cortexd.json"
    record: lifecycle.LifecycleRecord | None = None
    try:
        manager.start(timeout=8)
        record = lifecycle._read_record(state_path)
        assert record is not None
        with monkeypatch.context() as patch:
            patch.setattr(
                lifecycle,
                "_process_details",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    lifecycle.LifecycleError("process observation failed")
                ),
            )
            with pytest.raises(lifecycle.LifecycleError, match="observation failed"):
                manager.stop(timeout=1)
            assert state_path.exists()
            assert metadata_path.exists()
    finally:
        if not state_path.exists() and record is not None:
            lifecycle._atomic_private_json(state_path, record.as_json())
        manager.stop(timeout=5)


def test_process_observation_uses_the_remaining_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    observed_timeouts: list[float] = []

    def timeout_run(*_args: object, timeout: float, **_kwargs: object) -> object:
        observed_timeouts.append(timeout)
        time.sleep(min(timeout + 0.01, 0.2))
        raise subprocess.TimeoutExpired(["/bin/ps"], timeout)

    monkeypatch.setattr(lifecycle.subprocess, "run", timeout_run)
    process = lifecycle.ProcessClaim(1234, "start", "claim")
    started = time.monotonic()
    with pytest.raises(lifecycle.LifecycleError, match="observation failed"):
        lifecycle._wait_identity_exit(process, deadline=started + 0.05)
    assert time.monotonic() - started < 0.15
    assert observed_timeouts == [pytest.approx(0.05, abs=0.02)]


def test_json_request_bounds_total_elapsed_time_for_a_drip_response() -> None:
    import distribution.lifecycle as lifecycle

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    payload = b'{"items":[],"next_cursor":null}'

    def drip_response() -> None:
        connection, _ = listener.accept()
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            )
            for byte in payload:
                time.sleep(0.03)
                connection.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            connection.close()
            listener.close()

    server = threading.Thread(target=drip_response)
    server.start()
    started = time.monotonic()
    result = lifecycle._json_request(port, "/api/cortex/workspaces", timeout=0.05)
    elapsed = time.monotonic() - started
    server.join(timeout=1)

    assert elapsed < 0.2
    assert result is None
    assert not server.is_alive()


def test_json_request_bounds_total_elapsed_time_for_drip_headers() -> None:
    import distribution.lifecycle as lifecycle

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    def drip_response() -> None:
        connection, _ = listener.accept()
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            for byte in response:
                time.sleep(0.03)
                connection.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            connection.close()
            listener.close()

    server = threading.Thread(target=drip_response)
    server.start()
    started = time.monotonic()
    result = lifecycle._json_request(port, "/healthz", timeout=0.05)
    elapsed = time.monotonic() - started
    server.join(timeout=1)

    assert elapsed < 0.2
    assert result is None
    assert not server.is_alive()


def test_front_door_listener_observation_uses_the_remaining_probe_deadline(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    manager = lifecycle.LifecycleManager(
        generation,
        tmp_path / "lifecycle",
        home=tmp_path / "home",
    )
    record = lifecycle.LifecycleRecord(
        generation_identity=manager.generation.identity,
        generation_root=str(manager.generation.root),
        run_id="0" * 32,
        supervisor=lifecycle.ProcessClaim(1234, "start", "s" * 64),
        control=lifecycle.ControlClaim(
            1235,
            "start",
            "c" * 64,
            "127.0.0.1",
            8765,
            "c" * 64,
        ),
        web=lifecycle.WebClaim(
            1236,
            "start",
            "w" * 64,
            "127.0.0.1",
            8766,
            manager.generation.release_build_id,
        ),
    )
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle, "_read_record", lambda _path: record)
    monkeypatch.setattr(lifecycle, "_identity_matches", lambda *_args, **_kwargs: True)
    observed_timeouts: list[float] = []
    real_run = subprocess.run

    def timeout_run(*args: object, timeout: float, **kwargs: object) -> object:
        command = list(args[0]) if args else []
        if "--no-warnings" in command:
            # Only the listener observation is made to time out here; verifying
            # the generation still really parses its Web payload.
            return real_run(command, timeout=timeout, **kwargs)
        observed_timeouts.append(timeout)
        time.sleep(min(timeout + 0.01, 0.2))
        raise subprocess.TimeoutExpired(["/usr/sbin/lsof"], timeout)

    monkeypatch.setattr(lifecycle.subprocess, "run", timeout_run)
    started = time.monotonic()
    assert manager.probe_local_front_door(timeout=0.05) == lifecycle.LocalFrontDoorHealth(
        healthy=False,
        web_url=None,
        category="claims_unhealthy",
    )
    assert time.monotonic() - started < 0.15
    assert observed_timeouts == [pytest.approx(0.05, abs=0.02)]
    assert not lifecycle._listener_owned(1234, 8765, deadline=started)
    assert len(observed_timeouts) == 1


def test_shutdown_message_uses_one_absolute_read_deadline() -> None:
    import distribution.lifecycle as lifecycle

    reader, writer = socket.socketpair()

    def drip_response() -> None:
        try:
            for byte in b"{}\n":
                time.sleep(0.03)
                writer.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            writer.close()

    sender = threading.Thread(target=drip_response)
    sender.start()
    started = time.monotonic()
    try:
        with pytest.raises(lifecycle.LifecycleError, match="timed out"):
            lifecycle._read_shutdown_message(reader, deadline=started + 0.05)
        assert time.monotonic() - started < 0.15
    finally:
        reader.close()
        sender.join(timeout=1)


def test_stop_bounds_lifecycle_lock_wait_within_the_operation_deadline(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import fcntl

    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(generation, runtime, home=tmp_path / "home")
    manager.start(timeout=8)
    descriptor = os.open(runtime / ".lifecycle.lock", os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release() -> None:
        time.sleep(0.5)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    releaser = threading.Thread(target=release)
    releaser.start()
    started = time.monotonic()
    try:
        with pytest.raises(LifecycleError, match="lifecycle lock timed out"):
            manager.stop(timeout=0.1)
        assert time.monotonic() - started < 0.35
        assert (runtime / "lifecycle.json").exists()
    finally:
        releaser.join(timeout=2)
        manager.stop(timeout=5)


def test_metadata_cleanup_bounds_lock_wait_and_retains_lifecycle_authority(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    import fcntl
    import distribution.lifecycle as lifecycle

    from distribution.lifecycle import LifecycleError, LifecycleManager

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    runtime = tmp_path / "runtime"
    manager = LifecycleManager(generation, runtime, home=tmp_path / "home")
    manager.start(timeout=8)
    record = lifecycle._read_record(runtime / "lifecycle.json")
    assert record is not None
    lock_path = runtime / "control-state" / ".cortexd.metadata.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release() -> None:
        time.sleep(0.5)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    releaser = threading.Thread(target=release)
    releaser.start()
    started = time.monotonic()
    try:
        with pytest.raises(LifecycleError, match="metadata lock timed out"):
            lifecycle._remove_matching_control_metadata(
                runtime / "control-state" / "cortexd.json",
                record.control,
                deadline=time.monotonic() + 0.1,
            )
        assert time.monotonic() - started < 0.35
        assert (runtime / "lifecycle.json").exists()
    finally:
        releaser.join(timeout=2)
        manager.stop(timeout=5)


def test_schema2_product_manifest_binds_the_embedded_interpreter_digest(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import load_generation

    generation = _installed_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, embedded_python=True
    )

    installed = load_generation(generation)

    interpreter = generation / "python-runtime" / "bin" / "python3.14"
    digest = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    manifest = json.loads((generation / "product-manifest.json").read_text())
    # A newly composed generation carries whatever the current code composes;
    # the binding branch is `>= 2`, so this asserts the composed version rather
    # than a number that has to be edited every time the contract grows.
    assert manifest["schema_version"] == PRODUCT_MANIFEST_SCHEMA >= 2
    assert manifest["python_runtime"]["interpreter_sha256"] == digest
    assert manifest["python_runtime"]["venv_interpreter_sha256"] == digest
    assert installed.identity == hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_digest": installed.bundle_digest,
                "product_manifest_sha256": hashlib.sha256(
                    (generation / "product-manifest.json").read_bytes()
                ).hexdigest(),
                "processes": {
                    "control": hashlib.sha256(
                        (generation / "runtime" / "bin" / "cortexd").read_bytes()
                    ).hexdigest(),
                    "node": hashlib.sha256(
                        (generation / "node-runtime" / "bin" / "node").read_bytes()
                    ).hexdigest(),
                    "python": digest,
                    "web_adapter": hashlib.sha256(
                        (generation / "web" / "server" / "node-adapter.mjs").read_bytes()
                    ).hexdigest(),
                },
            }
        )
    ).hexdigest()


def test_generation_identity_is_unchanged_for_a_legacy_product_manifest(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """A published schema-1 generation must keep its exact identity input."""

    from distribution.lifecycle import load_generation

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)

    installed = load_generation(generation)

    assert installed.identity == hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_digest": installed.bundle_digest,
                "product_manifest_sha256": hashlib.sha256(
                    (generation / "product-manifest.json").read_bytes()
                ).hexdigest(),
                "processes": {
                    "control": hashlib.sha256(
                        (generation / "runtime" / "bin" / "cortexd").read_bytes()
                    ).hexdigest(),
                    "node": hashlib.sha256(
                        (generation / "node-runtime" / "bin" / "node").read_bytes()
                    ).hexdigest(),
                    "web_adapter": hashlib.sha256(
                        (generation / "web" / "server" / "node-adapter.mjs").read_bytes()
                    ).hexdigest(),
                },
            }
        )
    ).hexdigest()


def test_generation_rejects_a_venv_python_that_is_not_the_staged_interpreter(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, load_generation

    generation = _installed_generation(
        tmp_path,
        wheel_pair,
        web_closure,
        analyser_node,
        embedded_python=True,
        venv_interpreter=b"#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n",
    )

    with pytest.raises(LifecycleError, match="Python runtime digest"):
        load_generation(generation)


def test_generation_rejects_a_missing_embedded_interpreter(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import LifecycleError, load_generation

    generation = _installed_generation(
        tmp_path, wheel_pair, web_closure, analyser_node, embedded_python=True
    )
    (generation / "python-runtime" / "bin" / "python3.14").unlink()

    with pytest.raises(LifecycleError, match="generation path is unavailable"):
        load_generation(generation)


# ⟦P7⟧ ---------------------------------------------------------------------
# A fixed loopback port and one public door, plumbed from `[web]` through the
# installed generation's own interpreter into the adapter's environment.


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _install_real_config_module(generation: Path) -> None:
    """Give the fixture's installed site the real `config.py` beside `paths.py`."""

    product = generation / "runtime" / "installed-site" / "cortex_platform" / "product"
    (product / "config.py").write_text(
        (REPOSITORY / "cortex_platform" / "product" / "config.py").read_text()
    )


def test_generation_web_environment_carries_the_fixed_port_and_the_public_door(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    from distribution.lifecycle import load_generation
    from distribution.web_settings import InstalledWebSettings

    layout = load_generation(
        _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    )
    fixed = layout.web_environment(
        control_port=8799,
        control_token="C" * 43,
        bootstrap_token="B" * 43,
        web=InstalledWebSettings(port=8787),
    )
    assert fixed["CORTEX_WEB_LISTEN_PORT"] == "8787"
    assert not {"CORTEX_PUBLIC_ORIGIN", "CORTEX_ACCESS_ISSUER", "CORTEX_ACCESS_AUDIENCE"} & set(fixed)

    public = layout.web_environment(
        control_port=8799,
        control_token="C" * 43,
        bootstrap_token="B" * 43,
        web=InstalledWebSettings(
            port=8787,
            public_origin="https://cortex.example.test",
            access_issuer="https://example-team.cloudflareaccess.com",
            access_audience="ab" * 32,
        ),
    )
    assert public["CORTEX_WEB_LISTEN_PORT"] == "8787"
    assert public["CORTEX_PUBLIC_ORIGIN"] == "https://cortex.example.test"
    assert public["CORTEX_ACCESS_ISSUER"] == "https://example-team.cloudflareaccess.com"
    assert public["CORTEX_ACCESS_AUDIENCE"] == "ab" * 32
    # Everything else is the environment of before, key for key.
    unchanged = {
        name: value
        for name, value in public.items()
        if name not in {"CORTEX_PUBLIC_ORIGIN", "CORTEX_ACCESS_ISSUER", "CORTEX_ACCESS_AUDIENCE"}
    }
    assert unchanged == {
        **layout.web_environment(
            control_port=8799, control_token="C" * 43, bootstrap_token="B" * 43
        ),
        "CORTEX_WEB_LISTEN_PORT": "8787",
    }


def test_web_readiness_must_report_the_configured_port() -> None:
    import distribution.lifecycle as lifecycle

    ready = {
        "adapter_version": 1,
        "build_id": "cortex-r0-build-1",
        "event": "ready",
        "host": "127.0.0.1",
        "port": 4321,
        "service": "cortex-web",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", f"import json,time;print(json.dumps({ready!r}),flush=True);time.sleep(5)"],
        stdout=subprocess.PIPE,
    )
    try:
        with pytest.raises(lifecycle.LifecycleError, match="instead of the configured 8787"):
            lifecycle._await_web(
                process,
                claim="c" * 64,
                start_token="0",
                build_id="cortex-r0-build-1",
                deadline=time.monotonic() + 5,
                cancel=threading.Event(),
                expected_port=8787,
            )
    finally:
        process.kill()
        process.wait()


def test_web_health_tolerates_the_public_door_flag_and_nothing_else() -> None:
    import distribution.lifecycle as lifecycle

    body = {
        "adapter_version": 1,
        "build_id": "cortex-r0-build-1",
        "service": "cortex-web",
        "status": "ok",
    }
    assert lifecycle._web_health_matches(body, "cortex-r0-build-1") is True
    assert lifecycle._web_health_matches({**body, "public_door": True}, "cortex-r0-build-1")
    assert lifecycle._web_health_matches({**body, "public_door": False}, "cortex-r0-build-1")
    assert not lifecycle._web_health_matches({**body, "public_door": "yes"}, "cortex-r0-build-1")
    assert not lifecycle._web_health_matches({**body, "extra": True}, "cortex-r0-build-1")
    assert not lifecycle._web_health_matches({**body, "build_id": "other"}, "cortex-r0-build-1")


@pytest.mark.parametrize("holder_address", ["127.0.0.1", "0.0.0.0"])
def test_a_fixed_web_port_that_is_held_fails_typed(holder_address: str) -> None:
    """A loopback holder and a wildcard holder are both the named conflict.

    BSD lets `127.0.0.1:<port>` bind beside a `0.0.0.0:<port>` listener under
    `SO_REUSEADDR`, so a loopback-only probe reported a wildcard holder as
    free and `start` went on to bind a co-listener in silence.
    """

    import distribution.lifecycle as lifecycle

    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind((holder_address, 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(lifecycle.LifecycleError, match=f"port {port} is already in use"):
            lifecycle._require_free_web_port(port)
    finally:
        holder.close()
    lifecycle._require_free_web_port(port)


def test_a_time_wait_socket_is_not_a_port_conflict() -> None:
    import distribution.lifecycle as lifecycle

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _peer = listener.accept()
    accepted.close()
    client.close()
    listener.close()
    lifecycle._require_free_web_port(port)


def test_a_fixed_web_port_survives_a_restart_and_a_conflict_is_typed(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """`[web] port` reaches the listener through the real supervisor path.

    The installed site gains the real `config.py` so the generation's own
    interpreter answers the `[web]` question the way a real installation
    does; the Web stand-in binds whatever `CORTEX_WEB_LISTEN_PORT` says.
    """

    import distribution.lifecycle as lifecycle

    generation = _installed_generation(tmp_path, wheel_pair, web_closure, analyser_node)
    _install_real_config_module(generation)
    runtime_root = tmp_path / "lifecycle"
    runtime_root.mkdir(mode=0o700)
    (runtime_root / "config").mkdir(mode=0o700)
    port = _free_loopback_port()
    (runtime_root / "config" / "config.toml").write_text(
        f"config_version = 1\n\n[web]\n\"port\" = {port}\n"
    )
    manager = lifecycle.LifecycleManager(generation, runtime_root, home=tmp_path / "home")
    assert manager.web_settings.port == port
    assert manager.web_settings.public_door is False
    try:
        first = manager.start(timeout=8)
        assert first.state == "running"
        assert first.web_port == port
        assert first.web_port_fixed is True
        assert first.public_origin is None
        status = manager.status()
        assert (status.web_port, status.web_port_fixed) == (port, True)
        assert _json_get(port, "/_cortex/health")[0] == 200

        assert manager.stop(timeout=8).state == "stopped"
        assert _port_is_closed(port)
        second = manager.start(timeout=8)
        assert second.web_port == port, "a restart must keep the configured port"
        assert manager.stop(timeout=8).state == "stopped"

        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            with pytest.raises(
                lifecycle.LifecycleError, match=f"port {port} is already in use"
            ):
                manager.start(timeout=8)
        finally:
            holder.close()
        assert manager.status().state == "stopped"
        assert not (runtime_root / "lifecycle.json").exists()
    finally:
        manager.stop(timeout=8)
