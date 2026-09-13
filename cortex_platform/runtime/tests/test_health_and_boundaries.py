from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from cortex_platform.runtime.compatibility import (
    COMPATIBILITY_MATRIX,
    SUPPORTED_HERMES_DISTRIBUTIONS,
    SUPPORTED_SESSION_DB_SCHEMA,
)
from cortex_platform.runtime.hermes import HermesAdapter, HermesUnavailableError
from cortex_platform.runtime.models import HealthStatus

from .fakes import FakeHermesBackend


def test_hermes_modules_are_lazy_loaded() -> None:
    loader_calls = 0

    def loader():
        nonlocal loader_calls
        loader_calls += 1
        return FakeHermesBackend()

    HermesAdapter(backend_loader=loader)
    assert loader_calls == 0

    hermes_path = Path(__file__).resolve().parents[1] / "hermes.py"
    tree = ast.parse(hermes_path.read_text(encoding="utf-8"))
    top_level_modules = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level_modules.append(node.module or "")
    assert not {
        "hermes_state",
        "run_agent",
        "tools.terminal_tool",
    }.intersection(top_level_modules)


def test_health_reports_healthy_and_incompatible_backends() -> None:
    async def scenario() -> None:
        healthy = await HermesAdapter(
            backend_loader=lambda: FakeHermesBackend()
        ).health()
        assert healthy.status == HealthStatus.HEALTHY
        assert healthy.capabilities.pause is False
        assert healthy.compatibility["session_db_schema"] == 13

        incompatible = await HermesAdapter(
            backend_loader=lambda: FakeHermesBackend(compatible=False)
        ).health()
        assert incompatible.status == HealthStatus.INCOMPATIBLE
        assert incompatible.reason_code == "hermes_incompatible"
        assert incompatible.capabilities.available is False

    asyncio.run(scenario())


def test_health_probe_failure_is_sanitized_and_fail_soft() -> None:
    class BrokenProbeBackend(FakeHermesBackend):
        def compatibility(self):
            raise RuntimeError("/private/state.db?token=do-not-leak")

    async def scenario() -> None:
        health = await HermesAdapter(
            backend_loader=lambda: BrokenProbeBackend()
        ).health()
        assert health.status == HealthStatus.DEGRADED
        assert health.reason_code == "hermes_probe_failed"
        assert "do-not-leak" not in repr(health)

    asyncio.run(scenario())


def test_missing_hermes_degrades_without_leaking_exception_details() -> None:
    secret_detail = "/private/production/state.db?token=do-not-leak"

    def missing_backend():
        raise HermesUnavailableError(secret_detail)

    async def scenario() -> None:
        adapter = HermesAdapter(backend_loader=missing_backend)
        health = await adapter.health()
        capabilities = await adapter.capabilities()
        assert health.status == HealthStatus.UNAVAILABLE
        assert health.reason_code == "hermes_not_installed"
        assert capabilities.available is False
        assert secret_detail not in repr(health)

    asyncio.run(scenario())


def test_compatibility_matrix_is_pinned_to_verified_fork_surface() -> None:
    assert SUPPORTED_HERMES_DISTRIBUTIONS == ("0.15.0",)
    assert SUPPORTED_SESSION_DB_SCHEMA == (13, 13)
    assert "v2026.5.28" in COMPATIBILITY_MATRIX["fork"]
    assert COMPATIBILITY_MATRIX["control"]["pause"].startswith("unsupported")
    assert COMPATIBILITY_MATRIX["control"]["decision_wait"].startswith("explicit")
    assert "allow_permanent" in COMPATIBILITY_MATRIX["callback_shapes"][
        "approval_callback"
    ]
    assert set(COMPATIBILITY_MATRIX["callback_shapes"]) >= {
        "tool_progress_callback",
        "tool_start_callback",
        "tool_complete_callback",
        "stream_callback",
        "approval_callback",
    }


def test_research_modules_do_not_import_hermes_internals() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    research_root = repository_root / "profiles/research/src/cortex_research"
    assert research_root.is_dir()
    forbidden_roots = {"hermes_state", "hermes_cli", "run_agent", "agent", "gateway"}
    violations = []
    for path in research_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module.split(".", 1)[0] in forbidden_roots:
                    violations.append(f"{path}:{node.lineno}:{module}")
    assert violations == []
