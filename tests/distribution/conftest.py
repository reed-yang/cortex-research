from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def analyser_node() -> Path:
    configured = os.environ.get("CORTEX_TEST_NODE")
    candidate = Path(configured) if configured else Path(shutil.which("node") or "")
    node = candidate.resolve()
    if not node.is_file() or not os.access(node, os.X_OK):
        pytest.fail("distribution tests require a real Node executable; set CORTEX_TEST_NODE")
    return node


PYTHON_RUNTIME_RELATIVE = "vendor/python-runtime/cpython-3.14.6-cp314-macosx_11_0_arm64.tar.gz"
PYTHON_RUNTIME_PIN_RELATIVE = (
    "vendor/python-runtime/cpython-3.14.6-cp314-macosx_11_0_arm64.pin.json"
)


@pytest.fixture(scope="session")
def embedded_python_runtime() -> Path:
    configured = os.environ.get("CORTEX_TEST_PYTHON_RUNTIME")
    archive = Path(configured) if configured else REPOSITORY / PYTHON_RUNTIME_RELATIVE
    if not archive.is_file():
        pytest.fail(
            "distribution tests require the vendored embedded Python runtime; run "
            "`python -m tools.vendor_python_runtime` or set CORTEX_TEST_PYTHON_RUNTIME"
        )
    return archive.resolve()


@pytest.fixture(scope="session")
def embedded_python_pin() -> dict[str, object]:
    pin = REPOSITORY / PYTHON_RUNTIME_PIN_RELATIVE
    if not pin.is_file():
        pytest.fail(f"the committed embedded Python pin is missing at {PYTHON_RUNTIME_PIN_RELATIVE}")
    return json.loads(pin.read_text())


def make_wheel(
    directory: Path,
    name: str,
    version: str,
    package: str,
    *,
    requires: tuple[str, ...] = (),
    requires_python: str | None = None,
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
) -> Path:
    normalized = name.replace("-", "_")
    tag = f"{python_tag}-{abi_tag}-{platform_tag}"
    filename = f"{normalized}-{version}-{tag}.whl"
    wheel = directory / filename
    dist_info = f"{normalized}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    if requires_python is not None:
        metadata.append(f"Requires-Python: {requires_python}")
    metadata.extend(f"Requires-Dist: {value}" for value in requires)
    files = {
        f"{package}/__init__.py": f"VERSION = {version!r}\n",
        f"{dist_info}/METADATA": "\n".join(metadata) + "\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: cortex-distribution-test\n"
            "Root-Is-Purelib: true\n"
            f"Tag: {tag}\n"
        ),
        f"{dist_info}/RECORD": "",
    }
    if name == "cortex":
        files.update(
            {
                # The real bytes, not a stand-in: `verify_bundle` requires the
                # wheel's staging kernel to equal the one the bundle ships in
                # `tools/`, which is itself pinned to this checkout (⟦AMD-2b⟧).
                "cortex_platform/runtime_staging.py": (
                    REPOSITORY / "cortex_platform" / "runtime_staging.py"
                ).read_bytes(),
                "cortex_platform/product/__init__.py": "",
                "cortex_platform/product/control/__init__.py": "",
                "cortex_platform/product/control/schema.py": "SCHEMA_VERSION = 10\n",
                "cortex_platform/product/paths.py": (
                    "import os\n"
                    "from pathlib import Path\n"
                    "from types import SimpleNamespace\n"
                    "def resolve_paths():\n"
                    "    home = Path(os.environ['HOME'])\n"
                    "    support = home / 'Library' / 'Application Support' / 'Cortex'\n"
                    "    config = Path(os.environ.get('CORTEX_CONFIG_DIR', support))\n"
                    "    data = Path(os.environ.get('CORTEX_DATA_DIR', support / 'Data'))\n"
                    "    state = Path(os.environ.get('CORTEX_STATE_DIR', support / 'State'))\n"
                    "    cache = Path(os.environ.get('CORTEX_CACHE_DIR', home / 'Library' / 'Caches' / 'Cortex'))\n"
                    "    logs = Path(os.environ.get('CORTEX_LOG_DIR', home / 'Library' / 'Logs' / 'Cortex'))\n"
                    "    return SimpleNamespace(\n"
                    "        config_file=Path(os.environ.get('CORTEX_CONFIG_FILE', config / 'config.toml')),\n"
                    "        config_dir=config,\n"
                    "        data_dir=data,\n"
                    "        state_dir=state,\n"
                    "        cache_dir=cache,\n"
                    "        log_dir=logs,\n"
                    "        control_database_file=data / 'control.db',\n"
                    "        runtime_update_root=state / 'runtime-update',\n"
                    "    )\n"
                ),
                "cortex_platform/product/cli.py": "def main(): return 0\n",
                "cortex_platform/product/daemon.py": "def main(): return 0\n",
                "deployment/private_access/__init__.py": "",
                "deployment/private_access/cli.py": "def main(): return 0\n",
                "deployment/private_access/gateway.py": "def main(): return 0\n",
                "deployment/private_access/supervision.py": "def main(): return 0\n",
                f"{dist_info}/entry_points.txt": (
                    "[console_scripts]\n"
                    "cortex = cortex_platform.product.cli:main\n"
                    "cortexd = cortex_platform.product.daemon:main\n"
                    "cortex-private-access = deployment.private_access.cli:main\n"
                    "cortex-private-access-gateway = deployment.private_access.gateway:main\n"
                    "cortex-private-access-supervisor = deployment.private_access.supervision:main\n"
                ),
            }
        )
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return wheel


@pytest.fixture
def wheel_pair(tmp_path: Path) -> tuple[Path, Path]:
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    return (
        make_wheel(wheel_dir, "cortex", "1.0.0", "cortex_platform"),
        make_wheel(wheel_dir, "cortex-research", "1.0.0", "cortex_research"),
    )


@pytest.fixture
def web_closure(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "prebuilt-web"
    files = {
        "THIRD_PARTY_NOTICES.md": (REPOSITORY / "apps/web/THIRD_PARTY_NOTICES.md").read_bytes(),
        "licenses/OFL-1.1.txt": (REPOSITORY / "apps/web/licenses/OFL-1.1.txt").read_bytes(),
        "package.json": '{"private":true,"type":"module"}\n',
        "client/manifest.webmanifest": '{"name":"Cortex"}\n',
        "client/offline.html": "<!doctype html><title>Offline</title>\n",
        "client/sw.js": "self.addEventListener('fetch', () => {});\n",
        "client/assets/app.css": (REPOSITORY / "apps/web/app/fonts.css").read_bytes(),
        "client/assets/app.js": "export const ready = true;\n",
        "client/icons/cortex-180.png": b"png-180",
        "client/icons/cortex-192.png": b"png-192",
        "client/icons/cortex-512.png": b"png-512",
        "server/__vite_rsc_assets_manifest.js": "export default {};\n",
        "server/index.js": 'export default "cortex-r0-build-1";\n',
        "server/node-adapter.mjs": (
            'import handler from "./index.js";\n'
            "const ADAPTER_VERSION = 1;\n"
            "export { ADAPTER_VERSION, handler };\n"
        ),
        "server/ssr/__vite_rsc_assets_manifest.js": "export default {};\n",
        "server/ssr/index.js": "export default {};\n",
        "server/ssr/assets/runtime.js": "export default {};\n",
    }
    entries = []
    for relative, contents in sorted(files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = contents if isinstance(contents, bytes) else contents.encode()
        path.write_bytes(payload)
        entries.append(f"{hashlib.sha256(payload).hexdigest()}  {len(payload)}  {relative}")
    ledger = tmp_path / "web-payload.sha256"
    ledger.write_text("\n".join(entries) + "\n")
    return root, ledger
