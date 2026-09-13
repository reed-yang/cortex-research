"""The supported `cortex_research` surface is nine modules and five resources.

AR-MAIN plan section 3.1-3.3: the product reaches this package through exactly
one edge (`cortex_platform/product/engine/child.py`, lazily importing
`paper_ingest`), and the engine refuses every payload whose kind is not
`arxiv`. This file is the regression that keeps the surface from growing back:
a new module, a new resource, a re-added generic URL entry point or a new
`cortex_platform` back-edge turns the suite red instead of silently widening the
dependency and import closure again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cortex_research"

SUPPORTED_MODULES = {
    "__init__.py",
    "arxiv_client.py",
    "catalog.py",
    "chunker.py",
    "db.py",
    "embed.py",
    "index_papers.py",
    "paper_ingest.py",
    "radar_schema.py",
}

# Read by `db.apply_schema` (the first four) and `radar_schema.ensure_radar_schema`
# (the last). Nine `.py` files alone silently lose schema behaviour.
SUPPORTED_RESOURCES = {
    "paper_index_schema.sql",
    "ledger_schema.sql",
    "crux_schema.sql",
    "teaching_schema.sql",
    "radar_schema.sql",
}


def _package_files() -> set[str]:
    return {
        entry.name
        for entry in PACKAGE.iterdir()
        if entry.is_file() and not entry.name.endswith(".pyc")
    }


def test_the_package_is_exactly_the_supported_modules_and_resources() -> None:
    assert _package_files() == SUPPORTED_MODULES | SUPPORTED_RESOURCES


def test_the_package_has_no_subpackages() -> None:
    """`board/` and `mcp_servers/` are the excluded legacy applications."""

    directories = [
        entry.name
        for entry in PACKAGE.iterdir()
        if entry.is_dir() and entry.name != "__pycache__"
    ]
    assert directories == []


@pytest.mark.parametrize("name", sorted(SUPPORTED_RESOURCES))
def test_every_declared_resource_is_present_and_applies(name: str, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    assert (PACKAGE / name).is_file()


def test_apply_schema_reads_the_four_shared_resources(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex_research import db

    assert set(db._SCHEMAS) == SUPPORTED_RESOURCES - {"radar_schema.sql"}
    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "research.db"))
    connection = db.connect()
    try:
        db.apply_schema(connection)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    finally:
        connection.close()
    # One representative object per applied resource, so a dropped `.sql` cannot
    # pass by leaving the others in place.
    assert {"papers", "chunks"} <= names            # paper_index_schema.sql
    assert "relations" in names                     # ledger_schema.sql
    assert "cruxes" in names                        # crux_schema.sql
    assert "lessons" in names                       # teaching_schema.sql


def test_radar_schema_reads_its_own_resource(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex_research import db, radar_schema

    monkeypatch.setenv("CORTEX_RESEARCH_DB", str(tmp_path / "research.db"))
    connection = db.connect()
    try:
        radar_schema.ensure_radar_schema(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert "radar_signals" in names


def test_no_supported_module_imports_cortex_platform() -> None:
    """The nine modules import no `cortex_platform` code, at any nesting depth.

    This is what makes the legacy platform singletons (`llm`, `llm_sdk`,
    `chain_spec`, `budget_shim`, `telegram`, `adversarial`, `eval`, `memory`)
    removable: every back-edge to them ran through the excluded modules.
    """

    offenders: list[str] = []
    for name in sorted(SUPPORTED_MODULES):
        path = PACKAGE / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "cortex_platform":
                        offenders.append(f"{name}:{node.lineno} {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "cortex_platform":
                    offenders.append(f"{name}:{node.lineno} {node.module}")
    assert offenders == []


def test_no_supported_module_imports_an_excluded_sibling() -> None:
    """Relative and absolute intra-package imports stay inside the nine."""

    supported = {name.removesuffix(".py") for name in SUPPORTED_MODULES}
    offenders: list[str] = []
    for name in sorted(SUPPORTED_MODULES):
        path = PACKAGE / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.module:
                        targets.append(node.module.split(".")[0])
                    else:
                        targets.extend(alias.name.split(".")[0] for alias in node.names)
                elif node.module and node.module.split(".")[0] == "cortex_research":
                    parts = node.module.split(".")
                    targets.append(parts[1] if len(parts) > 1 else "")
                    if len(parts) == 1:
                        targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "cortex_research" and len(parts) > 1:
                        targets.append(parts[1])
            for target in targets:
                if target and target not in supported:
                    offenders.append(f"{name}:{node.lineno} {target}")
    assert offenders == []


def test_generic_url_ingestion_is_not_part_of_the_surface() -> None:
    """`ingest_html_url` / `ingest_pdf_url` were the whole legacy back-edge.

    They pulled in `url_hygiene`, `paper_title`, `detached_blog_brief`,
    `detached_run` and through them the ~140-module legacy tail plus
    `cortex_platform.{llm,chain_spec,eval,adversarial,llm_sdk,telegram}`. The
    product refuses every non-arXiv payload
    (`cortex_platform/product/engine/port.py`), so the entry points have no
    supported caller.
    """

    from cortex_research import paper_ingest

    for removed in (
        "ingest_html_url",
        "ingest_pdf_url",
        "normalize_source_url",
        "main",
    ):
        assert not hasattr(paper_ingest, removed), removed


def test_the_arxiv_entry_point_and_its_engine_facing_names_remain() -> None:
    from cortex_research import index_papers, paper_ingest

    for kept in ("ingest_arxiv", "_corpus_lookup", "_norm_id", "IngestError",
                 "TransientIngestError", "fetch_full_text", "_pdf_to_text"):
        assert hasattr(paper_ingest, kept), kept
    assert hasattr(index_papers, "index_paper")
    assert hasattr(index_papers, "build_index")


# Distributions declared in `profiles/research/pyproject.toml` (or claimed by R4)
# whose only consumers were excluded modules. Importing any of them again would
# silently re-widen the installed closure.
FORBIDDEN_THIRD_PARTY = {
    "anthropic", "claude_agent_sdk", "fastmcp", "fitz", "jinja2", "markupsafe",
    "openai", "readability", "starlette", "trafilatura", "uvicorn", "xhshow",
    "yaml",
}


def test_importing_the_whole_package_pulls_in_no_platform_or_legacy_dependency() -> None:
    """Measured in a clean interpreter, not inferred from the syntax tree.

    `-I` isolates the child from the developer's environment, so what it reports
    is what an installed wheel would import.
    """

    import json
    import subprocess
    import sys

    probe = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import cortex_research, cortex_research.paper_ingest, cortex_research.arxiv_client,"
        " cortex_research.index_papers, cortex_research.catalog, cortex_research.chunker,"
        " cortex_research.db, cortex_research.embed, cortex_research.radar_schema\n"
        "std = set(sys.stdlib_module_names)\n"
        "print(json.dumps({\n"
        "  'platform': sorted(n for n in sys.modules if n.split('.')[0] == 'cortex_platform'),\n"
        "  'third_party': sorted({n.split('.')[0] for n in set(sys.modules) - before\n"
        "     if n.split('.')[0] not in std and not n.startswith('_')\n"
        "     and n.split('.')[0] != 'cortex_research'}),\n"
        "}))\n"
    )
    repository = Path(__file__).resolve().parents[3]
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{repository}:{repository / 'profiles' / 'research' / 'src'}",
        "HOME": str(repository),  # never the operator's home
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env=environment, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed["platform"] == []
    assert not (set(observed["third_party"]) & FORBIDDEN_THIRD_PARTY), observed["third_party"]
    # httpx and sqlite_vec are module-scope imports of the kept code; bs4/lxml
    # arrive only when an arxiv HTML body is parsed, so they are not asserted here
    # (the engine-child runtime proof measures them).
    assert {"httpx", "sqlite_vec"} <= set(observed["third_party"]), observed["third_party"]


def test_the_readings_corpus_has_no_guessed_default(monkeypatch) -> None:
    """`CORTEX_AGENT_READINGS` or a refusal -- never one machine's directory.

    `agent_readings_papers` used to fall back to a home-relative corpus path,
    which the ingest write path (`paper_ingest.py:711`) would then create. The
    engine binds the variable on every child, so the fallback was reachable
    only outside the product -- exactly where writing into an invented
    directory is least recoverable.
    """

    from cortex_research import index_papers

    monkeypatch.delenv("CORTEX_AGENT_READINGS", raising=False)
    with pytest.raises(RuntimeError, match="CORTEX_AGENT_READINGS"):
        index_papers.agent_readings_papers()

    monkeypatch.setenv("CORTEX_AGENT_READINGS", "/tmp/corpus-under-test")
    assert index_papers.agent_readings_papers() == Path("/tmp/corpus-under-test/papers")
