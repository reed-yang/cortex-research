"""D3 layer: the AST scan owns the list, and drift is a test failure."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cortex_platform.product.engine.bindings import (
    DENIED,
    ENGINE_BINDINGS,
    RETAINED_WITHOUT_A_READER,
)
from cortex_platform.product.engine.conformance import (
    PLATFORM_MODULES,
    PROPAGATING_HELPERS,
    engine_package_root,
    platform_module_paths,
    scan_engine_surface,
    scan_environment_reads,
)


def test_every_ambient_read_the_engine_performs_is_dispositioned() -> None:
    """The assertion F3 argues for: an AST scan, not a grep, owns the list."""

    scanned = scan_engine_surface().names
    undispositioned = scanned - set(ENGINE_BINDINGS)
    assert not undispositioned, sorted(undispositioned)


def test_the_table_names_nothing_the_scan_and_the_reasons_do_not() -> None:
    """Totality in both directions, with the second direction now explicit.

    The registry half of this equality used to come from `chain_spec.ENV_REGISTRY`,
    which described the legacy model-dispatch chain and left with it. Its
    replacement is not "anything the table happens to carry": every row the scan
    does not find has to be named in `RETAINED_WITHOUT_A_READER` with a reason,
    so the table cannot quietly re-accumulate dispositions for code the product
    does not ship.
    """

    scanned = scan_engine_surface().names
    assert set(ENGINE_BINDINGS) == scanned | set(RETAINED_WITHOUT_A_READER)


def test_every_retained_row_without_a_reader_carries_a_reason() -> None:
    for name, reason in RETAINED_WITHOUT_A_READER.items():
        assert name in ENGINE_BINDINGS, name
        assert reason.strip(), name


def test_no_credential_is_retained_without_a_reader() -> None:
    """A secret bound for nobody is the failure this split exists to prevent."""

    for name in RETAINED_WITHOUT_A_READER:
        assert ENGINE_BINDINGS[name].secret_alias is None, name


def test_the_frozen_helper_set_is_still_present() -> None:
    """A rename must not silently shrink the scan.

    Five of the six helpers AMD-9 froze lived in excluded modules and left with
    them; `_env_cred` is the one the supported surface still has, and it is the
    hop that recovers the OCR credentials.
    """

    result = scan_engine_surface()
    assert PROPAGATING_HELPERS <= set(result.helpers)


def test_one_hop_propagation_recovers_the_credentials_a_literal_scan_hides() -> None:
    result = scan_engine_surface()
    by_hop = {read.name: read.hop for read in result.reads if read.hop != "literal"}
    for name in ("NOVITA_API_KEY", "GLM_API_ID", "GLM_API_KEY"):
        assert name in by_hop, name
        assert name in ENGINE_BINDINGS


def test_a_new_unbound_read_in_the_engine_fails_the_scan(tmp_path: Path) -> None:
    """The regression the table exists to catch, proven on a synthetic module."""

    module = tmp_path / "drifted.py"
    module.write_text(
        textwrap.dedent(
            """
            import os

            VALUE = os.environ.get("CORTEX_A_BRAND_NEW_UNBOUND_READ")
            """
        ),
        encoding="utf-8",
    )
    scanned = scan_environment_reads([module]).names
    assert "CORTEX_A_BRAND_NEW_UNBOUND_READ" in scanned
    assert not scanned <= set(ENGINE_BINDINGS)


def test_the_scan_follows_a_helper_and_a_literal_loop(tmp_path: Path) -> None:
    module = tmp_path / "helper.py"
    module.write_text(
        textwrap.dedent(
            """
            import os

            def _env_cred(name):
                return os.environ.get(name) or ""

            def _server_env(*keys):
                return {k: os.environ[k] for k in keys if os.environ.get(k)}

            _env_cred("HOPPED_ONE")
            _server_env("HOPPED_TWO", "HOPPED_THREE")

            for key in ("LOOPED_ONE", "LOOPED_TWO"):
                os.environ.get(key)
            """
        ),
        encoding="utf-8",
    )
    result = scan_environment_reads([module])
    assert result.names == {
        "HOPPED_ONE",
        "HOPPED_TWO",
        "HOPPED_THREE",
        "LOOPED_ONE",
        "LOOPED_TWO",
    }


def test_the_scan_covers_the_engine_and_names_no_platform_import() -> None:
    """The engine surface is the research package and nothing else.

    `PLATFORM_MODULES` used to list seven legacy `cortex_platform` modules and
    `platform_module_paths()` raised for a missing one, which is what made
    `llm`/`chain_spec`/`eval` mandatory at import time. The supported research
    modules import no platform code, so the list is empty and the resolver stays
    -- an added import must be declared here, not discovered when a build fails.
    """

    assert engine_package_root().name == "cortex_research"
    assert PLATFORM_MODULES == ()
    assert platform_module_paths() == ()


def test_an_undeclared_platform_import_is_still_refused() -> None:
    """The mechanism the empty tuple would otherwise make untestable."""

    with pytest.raises(RuntimeError, match="not importable"):
        platform_module_paths(("cortex_platform.a_module_that_does_not_exist",))


def test_dynamic_sites_are_enumerated_with_a_reason() -> None:
    from cortex_platform.product.engine.bindings import DYNAMIC_ENVIRONMENT_SITES

    result = scan_engine_surface()
    assert len(DYNAMIC_ENVIRONMENT_SITES) >= 1
    for site in DYNAMIC_ENVIRONMENT_SITES:
        assert site.reason and site.expression
    # Every unresolved site the scan finds is named in the frozen enumeration,
    # by module and expression rather than by line, so moving code is not drift.
    frozen = {
        (site.module.rsplit("/", 1)[-1], site.expression)
        for site in DYNAMIC_ENVIRONMENT_SITES
    }
    found = {
        (read.location.rsplit(":", 1)[0], read.expression) for read in result.dynamic
    }
    assert found <= frozen, sorted(found - frozen)
