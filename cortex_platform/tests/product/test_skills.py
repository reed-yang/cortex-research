"""Operator skills: declaration, identity, acceptance and the surfaces that report them."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_platform.product import skills
from cortex_platform.product.cli import main
from cortex_platform.product.config import ConfigError, _render_config, validate_config
from cortex_platform.product.paths import resolve_paths

MANIFEST = """---
name: fake-ocr
description: >
  A folded description that mentions
  metadata: and cortex-capability: lint on continuation lines.
metadata:
  author: someone
  cortex-capability: ocr
  cortex-entry: "scripts/run.py"
  cortex-interpreter: '.venv/bin/python'  # a comment the parser drops
allowed-tools: Read
---

# Fake OCR

metadata:
  cortex-capability: lint
"""


def _package(root: Path, name: str = "fake-ocr", manifest: str = MANIFEST) -> Path:
    package = root / name
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(manifest, encoding="utf-8")
    (package / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (package / ".venv" / "bin").mkdir(parents=True)
    (package / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return package


@pytest.fixture()
def paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(state_dir=tmp_path / "state")


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "skills"
    directory.mkdir()
    return directory


def _config(root: Path) -> dict[str, object]:
    return {"config_version": 1, "skills": {"root": str(root)}}


def test_the_metadata_block_is_read_and_nothing_else(root: Path) -> None:
    package = _package(root)
    assert skills.read_metadata(package / "SKILL.md") == {
        "author": "someone",
        "cortex-capability": "ocr",
        "cortex-entry": "scripts/run.py",
        "cortex-interpreter": ".venv/bin/python",
    }


def test_unterminated_or_absent_frontmatter_declares_nothing(tmp_path: Path) -> None:
    unterminated = tmp_path / "a.md"
    unterminated.write_text("---\nmetadata:\n  cortex-capability: ocr\n", encoding="utf-8")
    plain = tmp_path / "b.md"
    plain.write_text("metadata:\n  cortex-capability: ocr\n", encoding="utf-8")
    assert skills.read_metadata(unterminated) == {}
    assert skills.read_metadata(plain) == {}


def test_accepting_makes_the_capability_ready_and_bindable(root: Path, paths) -> None:
    package = _package(root)
    config = _config(root)
    before = skills.resolve_capability(config, paths, "ocr")
    assert before.state == skills.NOT_ACCEPTED
    assert before.binding_values() == {}

    accepted = skills.accept(config, paths, "ocr")
    assert accepted.state == skills.READY
    assert accepted.binding_values() == {
        "ocr.entry": str(package / "scripts" / "run.py"),
        "ocr.interpreter": str(package / ".venv" / "bin" / "python"),
    }
    record = skills.acceptance_file(paths)
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    stored = json.loads(record.read_text(encoding="utf-8"))["capabilities"]["ocr"]
    assert stored["digest"] == accepted.digest
    assert stored["package"] == "fake-ocr"


def test_a_changed_package_is_no_longer_ready_until_accepted_again(root: Path, paths) -> None:
    package = _package(root)
    config = _config(root)
    skills.accept(config, paths, "ocr")
    # The 2026-09-14 incident: a sync tool put an older entry file in place.
    (package / "scripts" / "run.py").write_text("print('older')\n", encoding="utf-8")
    changed = skills.resolve_capability(config, paths, "ocr")
    assert changed.state == skills.CHANGED
    assert changed.binding_values() == {}
    assert str(package) not in changed.reason
    assert skills.accept(config, paths, "ocr").state == skills.READY


def test_environments_caches_backups_and_secrets_are_not_identity(root: Path) -> None:
    package = _package(root)
    digest = skills.package_digest(package)
    (package / ".venv" / "lib").mkdir()
    (package / ".venv" / "lib" / "site.py").write_text("x", encoding="utf-8")
    (package / "scripts" / "__pycache__").mkdir()
    (package / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\0")
    (package / "scripts" / ".agent-sync-backups").mkdir()
    (package / "scripts" / ".agent-sync-backups" / "run.py.bak").write_text("y", encoding="utf-8")
    (package / ".env").write_text("GLM_API_KEY=not-identity\n", encoding="utf-8")
    assert skills.package_digest(package) == digest
    (package / "uv.lock").write_text("changed dependencies\n", encoding="utf-8")
    assert skills.package_digest(package) != digest


def test_a_symlink_is_recorded_by_target_and_never_followed(root: Path, tmp_path: Path) -> None:
    package = _package(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("a", encoding="utf-8")
    (package / "linked").symlink_to(outside, target_is_directory=True)
    digest = skills.package_digest(package)
    (outside / "data.txt").write_text("b", encoding="utf-8")
    assert skills.package_digest(package) == digest
    (package / "linked").unlink()
    (package / "linked").symlink_to(tmp_path, target_is_directory=True)
    assert skills.package_digest(package) != digest


@pytest.mark.parametrize(
    ("entry", "interpreter", "state"),
    [
        ("../escape.py", ".venv/bin/python", skills.INVALID),
        ("/abs/run.py", ".venv/bin/python", skills.INVALID),
        ("scripts/missing.py", ".venv/bin/python", skills.INVALID),
        ("scripts/run.py", "../python", skills.INVALID),
        ("scripts/run.py", ".venv/bin/python3", skills.UNPREPARED),
    ],
)
def test_a_package_that_cannot_run_is_refused(
    root: Path, paths, entry: str, interpreter: str, state: str
) -> None:
    manifest = (
        "---\nname: fake-ocr\ndescription: d\nmetadata:\n"
        f"  cortex-capability: ocr\n  cortex-entry: {entry}\n"
        f"  cortex-interpreter: {interpreter}\n---\n"
    )
    _package(root, manifest=manifest)
    status = skills.resolve_capability(_config(root), paths, "ocr")
    assert status.state == state
    with pytest.raises(skills.SkillAcceptanceError):
        skills.accept(_config(root), paths, "ocr")


def test_an_entry_symlinked_out_of_the_package_is_invalid(root: Path, paths, tmp_path: Path) -> None:
    package = _package(root)
    (tmp_path / "elsewhere.py").write_text("print(1)\n", encoding="utf-8")
    (package / "scripts" / "run.py").unlink()
    (package / "scripts" / "run.py").symlink_to(tmp_path / "elsewhere.py")
    assert skills.resolve_capability(_config(root), paths, "ocr").state == skills.INVALID


def test_unconfigured_missing_and_ambiguous_bind_nothing(root: Path, paths) -> None:
    assert skills.resolve_capability({}, paths, "ocr").state == skills.UNCONFIGURED
    assert skills.resolve_capability(_config(root), paths, "ocr").state == skills.MISSING
    _package(root, "first")
    _package(root, "second")
    ambiguous = skills.resolve_capability(_config(root), paths, "ocr")
    assert ambiguous.state == skills.AMBIGUOUS
    assert "first" in ambiguous.reason and "second" in ambiguous.reason
    with pytest.raises(skills.SkillAcceptanceError):
        skills.accept(_config(root), paths, "ocr")


def test_an_unreadable_acceptance_record_accepts_nothing(root: Path, paths) -> None:
    _package(root)
    skills.accept(_config(root), paths, "ocr")
    skills.acceptance_file(paths).write_text("{not json", encoding="utf-8")
    assert skills.resolve_capability(_config(root), paths, "ocr").state == skills.NOT_ACCEPTED


def test_an_unknown_capability_is_reported_and_ignored(root: Path, paths) -> None:
    _package(root, manifest=MANIFEST.replace("cortex-capability: ocr", "cortex-capability: lint"))
    assert skills.unknown_capabilities(_config(root)) == (("fake-ocr", "lint"),)
    assert skills.resolve_capability(_config(root), paths, "ocr").state == skills.MISSING
    with pytest.raises(skills.SkillAcceptanceError, match="unknown capability"):
        skills.accept(_config(root), paths, "lint")


def test_the_skills_section_validates_and_round_trips(root: Path) -> None:
    config = validate_config(_config(root))
    assert config["skills"] == {"root": str(root)}
    assert f'[skills]\n"root" = "{root}"' in _render_config(config)
    for bad in ({"root": "relative/path"}, {"root": "/"}, {"root": "/a/../b"},
                {"root": str(root), "extra": "x"}):
        with pytest.raises(ConfigError):
            validate_config({"config_version": 1, "skills": bad})


def _environment(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg" / "config"),
        "XDG_DATA_HOME": str(home / "xdg" / "data"),
        "XDG_STATE_HOME": str(home / "xdg" / "state"),
        "XDG_CACHE_HOME": str(home / "xdg" / "cache"),
    }


def test_the_cli_reports_accepts_and_doctor_follows(tmp_path: Path, root: Path, capsys) -> None:
    environment = _environment(tmp_path / "home")
    assert main(["init"], environ=environment, platform=sys.platform) == 0
    assert main(["doctor"], environ=environment, platform=sys.platform) == 0
    assert "skills: none" in capsys.readouterr().out

    registry = resolve_paths(environ=environment, platform=sys.platform)
    with registry.config_file.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[skills]\nroot = "{root}"\n')
    _package(root)

    assert main(["skills", "status"], environ=environment, platform=sys.platform) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["capabilities"][0]["state"] == "not_accepted"

    assert main(["skills", "accept"], environ=environment, platform=sys.platform) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["capabilities"][0]["state"] == "ready"
    assert accepted["capabilities"][0]["digest"].startswith("sha256:")

    assert main(["doctor"], environ=environment, platform=sys.platform) == 0
    assert "skills: ocr ready: fake-ocr matches its accepted digest" in capsys.readouterr().out

    (root / "fake-ocr" / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
    # Advisory: drift is reported, and doctor still exits 0.
    assert main(["doctor"], environ=environment, platform=sys.platform) == 0
    assert "skills: ocr changed" in capsys.readouterr().out


def test_the_cli_refuses_to_accept_what_cannot_run(tmp_path: Path, root: Path, capsys) -> None:
    environment = _environment(tmp_path / "home")
    assert main(["init"], environ=environment, platform=sys.platform) == 0
    registry = resolve_paths(environ=environment, platform=sys.platform)
    with registry.config_file.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[skills]\nroot = "{root}"\n')
    capsys.readouterr()
    assert main(["skills", "accept"], environ=environment, platform=sys.platform) == 1
    assert "ocr is missing" in capsys.readouterr().err
