"""The staging kernel must import with `distribution/` absent from `sys.path`.

This is the whole reason the kernel was extracted. `cortex runtime stage` is a
wheel console script: the wheel packages only `cortex_platform` and
`deployment`, while `distribution` exists solely as loose bundle files that an
installed product never sees. A single `from distribution...` — or an import of
a `cortex_platform` module that itself reaches into `distribution` — would make
the primitive unimportable exactly where S3.2 needs it, and every in-repository
test would still pass, because a checkout has `distribution/` right there, and
this checkout also puts it on `sys.path` through an editable install.

The guard is therefore a subprocess that withholds the checkout and then
refuses to prove anything unless `distribution` really is unreachable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]

# `-I` already ignores PYTHONPATH and the user site directory; the checkout
# still arrives through the editable install's `.pth`, so it is withheld by
# path rather than by interpreter flag.
_PROBE = '''\
"""Import the wheel-shipped staging kernel with the checkout withheld."""

import importlib.util
import os
import sys

repository = os.path.realpath(sys.argv[1])
sys.path[:] = [
    entry for entry in sys.path if entry and os.path.realpath(entry) != repository
]
# Isolated mode does not prepend this script's directory, so the tree standing
# in for the installed wheel is added deliberately and is the only addition.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if importlib.util.find_spec("distribution") is not None:
    raise SystemExit("distribution is still reachable; the guard proves nothing")

import cortex_platform.runtime_staging as kernel

if "distribution" in sys.modules:
    raise SystemExit("importing the kernel pulled distribution in")

leaked = sorted(
    name
    for name, value in vars(kernel).items()
    if str(getattr(value, "__module__", "") or "").startswith("distribution")
)
if leaked:
    raise SystemExit(f"the kernel exposes distribution objects: {leaked}")

profile = kernel.DEFAULT_PROFILE
print(profile.name, profile.interpreter_relative, kernel.stage_python_runtime.__name__)
'''


def _wheel_shipped_tree(root: Path) -> Path:
    """Stage exactly what the wheel packages: the kernel and its package."""

    package = root / "cortex_platform"
    package.mkdir()
    for name in ("__init__.py", "runtime_staging.py"):
        (package / name).write_bytes((REPOSITORY / "cortex_platform" / name).read_bytes())
    return root


def test_the_staging_kernel_imports_without_distribution(tmp_path: Path) -> None:
    probe = _wheel_shipped_tree(tmp_path) / "probe.py"
    probe.write_text(_PROBE)

    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(probe), str(REPOSITORY)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "cp314 bin/python3.14 stage_python_runtime"


def test_the_kernel_source_names_no_distribution_import() -> None:
    source = (REPOSITORY / "cortex_platform" / "runtime_staging.py").read_text()

    # A `distribution` import inside a function body would survive the
    # subprocess guard above, which only imports the module.
    assert "import distribution" not in source
    assert "from distribution" not in source
    assert "from .schema" not in source


def test_distribution_still_publishes_the_kernel_surface() -> None:
    from cortex_platform import runtime_staging as kernel
    from distribution import product_manifest

    assert product_manifest.ProductManifestError is kernel.RuntimeStagingError
    assert product_manifest.PythonRuntime is kernel.PythonRuntime
    assert product_manifest.stage_python_runtime is kernel.stage_python_runtime
    assert product_manifest.probe_python_runtime is kernel.probe_python_runtime
    assert product_manifest._darwin_embedded_linkage is kernel._darwin_embedded_linkage
    assert product_manifest.PYTHON_SUPPORTED_RELEASE == (3, 14)
    assert product_manifest.PYTHON_INTERPRETER_RELATIVE == "bin/python3.14"
    assert product_manifest.PYTHON_LIBRARY_RELATIVE == "lib/libpython3.14.dylib"
    assert product_manifest.PYTHON_LIBRARY_ID == "@rpath/libpython3.14.dylib"
    assert product_manifest.PYTHON_EXPECTED_RPATHS == frozenset(
        {"@executable_path/../lib"}
    )
