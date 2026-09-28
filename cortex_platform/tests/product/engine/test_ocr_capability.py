"""A PDF-only paper through a real effect child and an accepted operator OCR skill.

The skill is a stand-in with the paper-ingestion command-line contract; the
child, the binding table, the acceptance check and the retained engine are the
real ones. Offline: arXiv is the recorded fixture server.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.product import skills
from cortex_platform.product.engine.bindings import EngineRoots
from cortex_platform.product.engine.supervisor import ResearchEffectSupervisor
from cortex_platform.product.secrets import SecretValue

from .arxiv_fixture import ArxivFixtureServer
from .conftest import ActivationGate

PDF_ONLY_PAPER = "2601.00043"

MANIFEST = """---
name: stand-in-ocr
description: Paper-ingestion command-line stand-in for the capability test.
metadata:
  cortex-capability: ocr
  cortex-entry: scripts/ingest_paper.py
  cortex-interpreter: .venv/bin/python
---
"""

# Refuses to produce a body unless the effect ran it the way the product
# promises: its own interpreter with -B, the OCR credential present and the
# embedding credential withheld.
ENTRY = r'''
import argparse, json, os, sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source")
parser.add_argument("--engine")
parser.add_argument("--output-dir")
parser.add_argument("--image-format")
args = parser.parse_args()
if "OPENROUTER_API_KEY" in os.environ or not os.environ.get("NOVITA_API_KEY"):
    sys.exit(3)
if not sys.flags.dont_write_bytecode:
    sys.exit(4)
paper = Path(args.output_dir) / "20260101-Stand_In"
(paper / "assets").mkdir(parents=True)
(paper / "assets" / "image_001.png").write_bytes(b"\x89PNG\r\n\x1a\nSTANDIN")
body = "# Stand In\n\n![Figure 1](./assets/image_001.png)\n\n" + "OCR body $x^2$ " * 60
(paper / "full_text-Stand_In.md").write_text(body, encoding="utf-8")
print(json.dumps({"status": "success", "engine_used": args.engine,
                  "markdown_path": str(paper / "full_text-Stand_In.md"),
                  "paper_dir": str(paper)}))
'''


@pytest.fixture
def arxiv() -> ArxivFixtureServer:
    with ArxivFixtureServer() as server:
        yield server


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    package = tmp_path / "skills" / "stand-in-ocr"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(MANIFEST, encoding="utf-8")
    (package / "scripts" / "ingest_paper.py").write_text(ENTRY, encoding="utf-8")
    (package / ".venv" / "bin").mkdir(parents=True)
    (package / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return package.parent


def _supervisor(roots, arxiv, config, product_paths) -> ResearchEffectSupervisor:
    return ResearchEffectSupervisor(
        store=ActivationGate(True),
        roots=roots,
        skip_embed=True,
        timeout_seconds=180,
        literal_overrides=arxiv.literal_overrides(),
        secret_provider=lambda: {
            "novita": SecretValue("novita", "fake-novita"),
            "openrouter": SecretValue("openrouter", "fake-embedding"),
        },
        capability_provider=lambda: skills.resolve_all(config, product_paths),
    )


def test_an_accepted_skill_ocrs_a_pdf_only_paper_into_the_corpus(
    tmp_path: Path, roots: EngineRoots, research_db: Path, arxiv, skill_root: Path
) -> None:
    config = {"skills": {"root": str(skill_root)}}
    product_paths = SimpleNamespace(state_dir=tmp_path / "product-state")
    skills.accept(config, product_paths, "ocr")

    execution = _supervisor(roots, arxiv, config, product_paths).run(
        "ingest_arxiv", {"identifier": PDF_ONLY_PAPER}
    )

    assert execution.ok, (execution.failure_category, execution.failure_message)
    assert execution.engine["full_text_source"] == "ocr"
    paper = roots.corpus_root / execution.paper_dirs[0]
    assert "OCR body" in (paper / "full_text.md").read_text(encoding="utf-8")
    assert (paper / "assets" / "image_001.png").is_file()
    assert execution.write_boundary["ok"] and execution.survivors.get("clean", True)
    # Nothing was written into the operator's skill.
    assert not list(skill_root.rglob("__pycache__"))


def test_a_skill_changed_after_acceptance_is_not_run(
    tmp_path: Path, roots: EngineRoots, research_db: Path, arxiv, skill_root: Path
) -> None:
    config = {"skills": {"root": str(skill_root)}}
    product_paths = SimpleNamespace(state_dir=tmp_path / "product-state")
    skills.accept(config, product_paths, "ocr")
    entry = skill_root / "stand-in-ocr" / "scripts" / "ingest_paper.py"
    entry.write_text(ENTRY + "\n# replaced by a sync tool\n", encoding="utf-8")

    execution = _supervisor(roots, arxiv, config, product_paths).run(
        "ingest_arxiv", {"identifier": PDF_ONLY_PAPER}
    )

    assert not execution.ok
    assert execution.failure_category == "capability_unavailable"
    assert "OCR capability changed" in execution.failure_message
    assert str(skill_root) not in execution.failure_message
    assert list(roots.corpus_root.iterdir()) == []
