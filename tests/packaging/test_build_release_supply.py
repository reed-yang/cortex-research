"""The build driver's reading of the wheelhouse's pure-wheel ledger.

`tools/vendor_wheelhouse.py` writes `built-from-sdist.json` only when it derived
a dependency wheel from that distribution's pinned sdist. A closure whose every
distribution publishes a wheel produces none, so the driver has to treat the
file's absence as a supply state rather than as a missing supply -- while still
refusing the one shape that claims a derivation and then names none.

The driver is a checkout-only script under `deployment/research_activation/`, so
it is loaded by path rather than imported as a package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER = REPO_ROOT / "deployment" / "research_activation" / "build_release.py"


def _driver():
    specification = importlib.util.spec_from_file_location(
        "cortex_build_release_under_test", DRIVER
    )
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        del sys.modules[specification.name]
    return module


build_release = _driver()


def _ledger(wheelhouse: Path, payload: str) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    path = wheelhouse / build_release.SDIST_LEDGER_NAME
    path.write_text(payload)
    return path


def test_a_wheelhouse_with_no_ledger_is_a_complete_supply(tmp_path: Path) -> None:
    """The research-only closure's own state: every distribution has a wheel."""

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    assert build_release.read_sdist_ledger(wheelhouse) == []


def test_a_ledger_naming_a_derived_wheel_is_reported(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    _ledger(
        wheelhouse,
        json.dumps(
            {
                "peewee": {"sdist_sha256": "a" * 64, "wheel_sha256": "b" * 64},
                "anyio": {"sdist_sha256": "c" * 64, "wheel_sha256": "d" * 64},
            }
        ),
    )

    assert build_release.read_sdist_ledger(wheelhouse) == ["anyio", "peewee"]


def test_a_present_but_empty_ledger_is_refused(tmp_path: Path) -> None:
    """`{}` is not "no sdist builds" -- that state is the file's absence.

    It is also what a stale file looks like after the producer stopped writing
    one, so accepting it would let a build pass on a supply nobody explained.
    `distribution/bundle.py` refuses the same shape inside a bundle.
    """

    wheelhouse = tmp_path / "wheelhouse"
    _ledger(wheelhouse, "{}")

    with pytest.raises(ValueError, match="explains no wheel"):
        build_release.read_sdist_ledger(wheelhouse)


@pytest.mark.parametrize("payload", ["", "[]", "null", "not json"])
def test_an_unreadable_ledger_is_refused(payload: str, tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    _ledger(wheelhouse, payload)

    with pytest.raises(ValueError):
        build_release.read_sdist_ledger(wheelhouse)


def test_the_ledger_is_not_one_of_the_required_supply_files() -> None:
    """Preflight must not demand a file a pure-wheel acquisition never writes."""

    source = DRIVER.read_text(encoding="utf-8")
    required = source[source.index("supplies = ["): source.index("missing = [")]

    assert "closure.requirements.txt" in required
    assert build_release.SDIST_LEDGER_NAME not in required
