"""The worker payload is the product's contract, shipped inside the release.

Three copies of the same bytes exist by the time a worker runs: the product's
sources under `worker_payload/`, the files `package_hermes_release.py` writes
into the payload, and the members `import_release` extracts into `content/`. The
last two are pinned in `tests/packaging`. This module pins the first: that the
shipped modules are not a second implementation of things the product already
defines, and that the entrypoint the launch contract executes obeys ⟦AMD-6⟧.

The same lesson as ⟦AMD-1b⟧ and ⟦AMD-2b⟧, a third time: when one file has to
live in two places, the copy is fine and the *unchecked* copy is not.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from cortex_platform.product.runtime_update import (
    operation_ledger,
    worker,
    worker_protocol,
)
from cortex_platform.product.runtime_update.models import canonical_json, digest_document
from cortex_platform.product.runtime_update.worker_payload import (
    ENTRYPOINT_SOURCE,
    SOURCE_ROOT,
    module_sources,
)
from cortex_platform.product.runtime_update.worker_payload.cortex_worker import (
    digests as payload_digests,
    ledger as payload_ledger,
    protocol as payload_protocol,
    serve as payload_serve,
)

PRODUCT_ROOT = Path(worker.__file__).resolve().parent
# The fork surface `_load_native_backend` names, and nothing else.
FORK_MODULES = {"hermes_state", "run_agent", "tools"}


@pytest.mark.parametrize(
    ("shipped", "product"),
    [
        ("cortex_worker/protocol.py", "worker_protocol.py"),
        ("cortex_worker/ledger.py", "operation_ledger.py"),
    ],
)
def test_the_copied_modules_are_byte_identical_to_their_product_sources(
    shipped: str, product: str
) -> None:
    """Two of the shipped modules are copies, so they are pinned as copies.

    Both are already stdlib-only with no intra-package imports, which is what
    makes a byte copy possible at all — and a byte copy is what makes drift
    impossible to introduce quietly. A change to either side that is not made to
    both fails here rather than at a customer's identity check.
    """

    assert (SOURCE_ROOT / shipped).read_bytes() == (PRODUCT_ROOT / product).read_bytes()


def test_the_shipped_protocol_module_is_the_same_contract() -> None:
    """Byte equality proved above; this states what the bytes have to mean."""

    assert payload_protocol.PROTOCOL_V2 == worker_protocol.PROTOCOL_V2
    assert payload_protocol._DESCRIPTOR_FIELDS == worker_protocol._DESCRIPTOR_FIELDS
    assert payload_protocol._METHOD_FIELDS == worker_protocol._METHOD_FIELDS
    assert payload_protocol._ERROR_CATEGORIES == worker_protocol._ERROR_CATEGORIES
    assert payload_ledger._RECORD_FIELDS == operation_ledger._RECORD_FIELDS
    assert payload_ledger._PHASES == operation_ledger._PHASES


@pytest.mark.parametrize(
    "document",
    [
        {},
        [],
        {"b": 1, "a": 2},
        {"nested": [{"z": None, "a": True}], "unicode": "π — ünïcode"},
        {"path": "runtime/cpython-3.11.15-cp311-macosx_11_0_arm64.tar.gz"},
        [{"path": "a", "kind": "directory"}, {"path": "b", "kind": "file", "mode": 292}],
        {"large": 2**63, "negative": -1, "float": 1.5},
    ],
)
def test_the_shipped_digest_helpers_agree_with_the_product(document: object) -> None:
    """`digests.py` is written rather than copied, so it is pinned behaviourally.

    It cannot be a copy: `models.py` carries the whole manifest schema and the
    worker must not import a schema it is not allowed to depend on. What has to
    hold is that the two produce the same bytes for the same document, because
    the worker's self-measured identity is compared for equality against a value
    the updater computed with the other one.
    """

    assert payload_digests.canonical_json(document) == canonical_json(document)
    assert payload_digests.digest_document(document) == digest_document(document)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_both_digest_helpers_refuse_the_same_unencodable_values(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"v": value})
    with pytest.raises(ValueError):
        payload_digests.canonical_json({"v": value})


def test_the_shipped_content_digest_matches_the_updater(tmp_path: Path) -> None:
    """The number a healthy slot depends on, computed by both implementations.

    A difference here — key order, mode masking, the shape of a directory entry —
    would turn every healthy slot into a permanent identity mismatch, and it would
    do so only in production, where the two are on opposite sides of a pipe. The
    updater is the counterparty now that S3.3 has retired the product-side twin;
    it always was the one that matters.
    """

    from cortex_platform.product.runtime_update.service import _tree_digest

    root = tmp_path / "content"
    (root / "package" / "nested").mkdir(parents=True)
    (root / "runtime_worker.py").write_text("def handle(m, p):\n    return {}\n")
    (root / "package" / "data.txt").write_text("payload\n")
    (root / "package" / "nested" / "binary.bin").write_bytes(bytes(range(256)))
    (root / "runtime_worker.py").chmod(0o444)

    assert payload_serve.content_tree_digest(root) == _tree_digest(root)


def test_the_shipped_interpreter_measurement_is_the_running_binary() -> None:
    """Both sides digest bytes, and both digest the same file."""

    import sys

    expected = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

    assert payload_serve.measure_interpreter() == expected


def test_the_entrypoint_bootstraps_from_its_own_resolved_directory() -> None:
    """⟦AMD-6⟧, read off the file the launch contract actually executes.

    `-I` removes `PYTHONPATH` and user site precisely so that the only way the
    worker package can be found is the one the entrypoint chooses. It chooses its
    own realpath'd directory — the sealed content root the slot's
    `content_tree_sha256` witnesses — and nothing else.
    """

    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]

    assert "os.path.dirname(os.path.realpath(__file__))" in body
    assert "sys.path.insert(0, directory)" in body
    assert "sys.dont_write_bytecode = True" in body
    # Never the working directory, never the environment.
    assert "PYTHONPATH" not in body
    assert "os.getcwd" not in body
    assert "os.environ" not in body
    assert "def handle(" in body


def test_every_shipped_module_is_stdlib_only() -> None:
    """The interpreter that imports these is the one the release carries.

    It has no `cortex_platform`, no site-packages of the product's, and `-I`
    removes anything the environment might have offered — so an import of
    anything outside the standard library and the package itself is a launch
    failure waiting for its first real run. Decided from the parsed AST rather
    than by scanning text, because the modules legitimately name
    `cortex_platform` in prose.
    """

    import ast
    import sys

    for relative, source in (
        {"runtime_worker.py": ENTRYPOINT_SOURCE} | module_sources()
    ).items():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative, i.e. inside the shipped package
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                # `cortex_worker` is the payload's own package, reached only
                # through the entrypoint's ⟦AMD-6⟧ bootstrap. The fork's own
                # top-level names are the one other allowance, and they are
                # enumerated rather than waved through: the slot's content root
                # is flat and generic, so "anything importable from there" would
                # be no rule at all. Every one of these is imported lazily,
                # inside `runtime.py`, after `HERMES_HOME` has been proved inert.
                assert (
                    root in sys.stdlib_module_names
                    or root == "cortex_worker"
                    or root in FORK_MODULES
                ), f"{relative}: {root}"
