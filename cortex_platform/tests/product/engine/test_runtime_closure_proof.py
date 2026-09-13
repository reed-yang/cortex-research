"""Runtime proof of the research boundary (AR-MAIN plan 10.6 / decision 9.1).

The nine-module target was derived statically. A static closure cannot see a
`-m` string, an `importlib` call or a template read, so this file proves the
same boundary by execution instead: it runs a real `ingest_arxiv` -- the exact
call `product/engine/child.py` makes, with `strict=True`, under the fully
replacing environment `bindings.research_effect_environment` builds -- inside a
child interpreter that audits itself.

Two properties are asserted from that run:

1. every `cortex_research` module that ends up in `sys.modules` is one of the
   nine, and the third-party distributions the run imports are the declared set;
2. no file under the legacy profile material
   (`profiles/research/{prompts,configs,config.yaml,skills,hooks,scripts,
   cron-scripts,journal}`) is opened, so those trees could be dropped without
   silently losing a template, prompt or configuration read. That proof is what
   authorized their removal from this branch (plan 10.4); the check stays as the
   guard against re-introducing a runtime read of profile material.

The audit hook is installed in the child, never in the pytest process: an audit
hook cannot be removed once added.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cortex_platform.product.engine.bindings import EngineRoots, research_effect_environment

from .arxiv_fixture import ArxivFixtureServer
from .conftest import build_research_db

REPOSITORY = Path(__file__).resolve().parents[4]
PROFILE_ROOT = REPOSITORY / "profiles" / "research"

# `10.4`: the trees removed from the branch once this proof held.
EXCLUDED_PROFILE_MATERIAL = (
    "prompts",
    "configs",
    "config.yaml",
    "skills",
    "hooks",
    "scripts",
    "cron-scripts",
    "journal",
)

SUPPORTED_MODULES = {
    "cortex_research",
    "cortex_research.arxiv_client",
    "cortex_research.catalog",
    "cortex_research.chunker",
    "cortex_research.db",
    "cortex_research.embed",
    "cortex_research.index_papers",
    "cortex_research.paper_ingest",
    "cortex_research.radar_schema",
}

# The distributions the supported ingest requires DIRECTLY. `lxml` is here even
# though no `import lxml` statement exists in the package: `paper_ingest` calls
# `BeautifulSoup(html, "lxml")`, a parser NAME that no import scan can see and
# that `bs4` resolves by importing `lxml` at parse time. The probe observes it,
# which is what turns "declare lxml" from an inference into a measurement.
EXPECTED_DIRECT_THIRD_PARTY = {"httpx", "sqlite_vec", "bs4", "lxml"}

# Declarations in `profiles/research/pyproject.toml` whose only consumers were
# excluded modules, plus the two undeclared arrivals R4 wanted to promote to
# dependencies. None may appear: `fitz`/PyMuPDF in particular is refused by the
# `strict=True` path rather than installed (plan 3.5).
FORBIDDEN_THIRD_PARTY = {
    "anthropic",
    "claude_agent_sdk",
    "fastmcp",
    "fitz",
    "jinja2",
    "markupsafe",
    "openai",
    "readability",
    "starlette",
    "trafilatura",
    "uvicorn",
    "xhshow",
    "yaml",
}

PROBE = r'''
import io, json, os, sys

opened = []
imported_order = []

def _hook(event, args):
    if event == "open":
        try:
            opened.append(str(args[0]))
        except Exception:
            pass
    elif event == "subprocess.Popen" or event == "os.exec":
        opened.append("EXEC:" + str(args[0]))

sys.addaudithook(_hook)

before = set(sys.modules)
from cortex_research.paper_ingest import (
    IngestError, TransientIngestError, _norm_id, ingest_arxiv,
)
result = ingest_arxiv(_norm_id(os.environ["PROBE_ARXIV_ID"]), source="product", strict=True)
after = set(sys.modules)

stdlib = set(sys.stdlib_module_names)
third_party = sorted(
    {
        name.split(".")[0]
        for name in after - before
        if name.split(".")[0] not in stdlib
        and not name.startswith("_")
        and name.split(".")[0] not in {"cortex_research", "cortex_platform"}
    }
)
research = sorted(name for name in after if name.split(".")[0] == "cortex_research")
platform = sorted(name for name in after if name.split(".")[0] == "cortex_platform")
print("PROBE_JSON:" + json.dumps({
    "ok": bool(result.get("ok")),
    "paper_dir": result.get("paper_dir"),
    "full_text_source": result.get("full_text_source"),
    "research": research,
    "platform": platform,
    "third_party": third_party,
    "opened": opened,
}))
'''


def _run_probe(tmp_path: Path, arxiv: ArxivFixtureServer, identifier: str) -> dict:
    registry_environ = {
        "HOME": str(tmp_path / "home"),
        "CORTEX_DATA_DIR": str(tmp_path / "data"),
        "CORTEX_STATE_DIR": str(tmp_path / "state"),
    }
    from cortex_platform.product.paths import resolve_paths

    registry = resolve_paths(environ=registry_environ, platform="darwin")
    roots = EngineRoots.resolve(
        registry, corpus_root=registry.data_dir / "research" / "corpus"
    )
    roots.corpus_root.mkdir(parents=True, exist_ok=True)
    roots.prepare()
    build_research_db(roots.research_db)

    environment = research_effect_environment(
        roots=roots,
        effect_marker="proof0",
        skip_embed=True,
        literal_overrides=dict(arxiv.literal_overrides()),
    )
    # The child resolves the two packages the way the installed wheel does; in a
    # checkout that means this repository, and nothing else may be inherited.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY), str(REPOSITORY / "profiles" / "research" / "src")]
    )
    environment["PROBE_ARXIV_ID"] = identifier
    completed = subprocess.run(
        [sys.executable, "-I", "-c", PROBE],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    line = [
        text for text in completed.stdout.splitlines() if text.startswith("PROBE_JSON:")
    ]
    assert line, completed.stdout + completed.stderr
    return json.loads(line[-1][len("PROBE_JSON:") :])


@pytest.fixture()
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


@pytest.fixture(scope="module")
def _probe_cache() -> dict:
    return {}


@pytest.fixture()
def probe(tmp_path: Path, arxiv: ArxivFixtureServer) -> dict:
    return _run_probe(tmp_path, arxiv, "2601.00042")


def test_the_supported_ingest_still_succeeds_under_the_bound_environment(probe: dict) -> None:
    assert probe["ok"] is True
    assert probe["full_text_source"] == "html"
    assert probe["paper_dir"]


def test_only_the_nine_supported_modules_are_imported_at_runtime(probe: dict) -> None:
    assert set(probe["research"]) <= SUPPORTED_MODULES, sorted(
        set(probe["research"]) - SUPPORTED_MODULES
    )
    # Not merely "no extras": the arxiv path really does reach all nine, which is
    # why none of them can be dropped from the export.
    assert set(probe["research"]) == SUPPORTED_MODULES


def test_the_engine_child_needs_no_cortex_platform_module(probe: dict) -> None:
    """The whole reason the legacy platform singletons are removable."""

    assert probe["platform"] == []


def test_every_directly_required_distribution_is_imported_at_runtime(probe: dict) -> None:
    """The list AR-P declares, measured rather than inferred."""

    observed = set(probe["third_party"])
    assert EXPECTED_DIRECT_THIRD_PARTY <= observed, sorted(
        EXPECTED_DIRECT_THIRD_PARTY - observed
    )


def test_no_removed_declaration_is_imported_at_runtime(probe: dict) -> None:
    """The declarations whose only consumer left with the excluded modules."""

    observed = set(probe["third_party"])
    assert not (observed & FORBIDDEN_THIRD_PARTY), sorted(observed & FORBIDDEN_THIRD_PARTY)


def test_the_run_reads_no_excluded_legacy_profile_material(probe: dict) -> None:
    """`prompts/`, `skills/`, `hooks/`, `configs/`, ... are never opened.

    Section 10.4 makes main's exclusion of those trees conditional on this: a
    template or prompt read at runtime would turn an excluded directory into a
    shipped resource.
    """

    offenders = []
    for opened in probe["opened"]:
        try:
            resolved = Path(opened).resolve()
        except (OSError, ValueError):
            continue
        for name in EXCLUDED_PROFILE_MATERIAL:
            candidate = PROFILE_ROOT / name
            if resolved == candidate or candidate in resolved.parents:
                offenders.append(opened)
    assert offenders == [], offenders


def test_the_run_opens_nothing_outside_the_bound_roots_and_the_installed_code(
    probe: dict, tmp_path: Path
) -> None:
    """Reads land in the product's own roots or in the code being executed.

    A legacy `~/gdrive/...` corpus default is what the binding table exists to
    displace, so a read under any path of that shape is the failure this
    asserts against.
    """

    for opened in probe["opened"]:
        assert "gdrive" not in opened, opened
