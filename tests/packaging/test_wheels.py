from __future__ import annotations

import configparser
import os
import subprocess
import tomllib
import zipfile
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from distribution.bundle import (
    _PRIVATE_ACCESS_MODULES,
    _PRIVATE_ACCESS_SCRIPTS,
    _WORKSPACE_DISTRIBUTIONS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_WHEEL_CONTENTS = {
    "cortex": {
        "cortex_platform/__init__.py",
        # The two modules that sit beside the package tree rather than inside a
        # subpackage, which an `only-include` of a directory can drop silently.
        # `runtime_staging.py` is the published runtime profile surface
        # (`distribution/product_manifest.py`) and `bundle.py` pins this wheel's
        # copy byte-equal to the one it stages under `tools/`; `backup.py` is
        # the database-inspection utility `tools/hermes_acceptance.py` imports.
        "cortex_platform/backup.py",
        "cortex_platform/runtime_staging.py",
        # The vendored worker payload ships as ordinary wheel members even
        # though nothing imports it in-process: the installer copies these bytes
        # into the worker slot and `distribution/release_record.py` attests them.
        "cortex_platform/product/runtime_update/worker_payload/__init__.py",
        "cortex_platform/product/runtime_update/worker_payload/runtime_worker.py",
        # The declared bundle process, plus its own configuration example --
        # the one non-Python member the root wheel still carries, and the reason
        # package data cannot be pruned to `*.py`.
        "deployment/private_access/cli.py",
        "deployment/private_access/example.config.json",
    },
    "cortex-research": {
        "cortex_research/__init__.py",
        "cortex_research/arxiv_client.py",
        "cortex_research/catalog.py",
        "cortex_research/chunker.py",
        "cortex_research/db.py",
        "cortex_research/embed.py",
        "cortex_research/index_papers.py",
        "cortex_research/paper_ingest.py",
        "cortex_research/radar_schema.py",
        # Package data, not documentation: `db.py:10,52-57` executes the first
        # four when they exist and `radar_schema.py:9,271` executes the last.
        # Nine modules without them lose schema behaviour silently.
        "cortex_research/paper_index_schema.sql",
        "cortex_research/ledger_schema.sql",
        "cortex_research/crux_schema.sql",
        "cortex_research/teaching_schema.sql",
        "cortex_research/radar_schema.sql",
    },
}

# The trees a published wheel may contain. `deployment/` is a namespace package
# with no `__init__.py`, so an exclude-only policy can never keep a NEW
# `deployment/<tool>/` directory out of the wheel; this allowlist can, and it is
# the assertion that fails when one appears.
EXPECTED_WHEEL_TREES = {
    "cortex": ("cortex_platform/", "deployment/private_access/"),
    "cortex-research": ("cortex_research/",),
}

# Categories that never ship. Each rule carries its own label so a failure names
# the policy that was broken, not just the file. `/tests/` alone is not enough:
# a test module that sits beside its subject, as the withdrawn
# `deployment/cc_shim/test_cortex_cc_shim.py` did, passes a `tests/`-only check
# for as long as it exists in the wheel.
FORBIDDEN_MEMBER_RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("test package", lambda member: "tests" in PurePosixPath(member).parts[:-1]),
    (
        "test module",
        lambda member: PurePosixPath(member).name.startswith("test_")
        and member.endswith(".py"),
    ),
    ("pytest fixture module", lambda member: PurePosixPath(member).name == "conftest.py"),
    ("readme", lambda member: PurePosixPath(member).name == "README.md"),
    ("planning document", lambda member: member.endswith("_PLAN.md")),
    ("build manifest", lambda member: PurePosixPath(member).name == "pyproject.toml"),
    (
        "compiled bytecode",
        lambda member: "__pycache__" in PurePosixPath(member).parts
        or member.endswith((".pyc", ".pyo")),
    ),
)

EXPECTED_DIRECT_RUNTIME_DEPENDENCIES = {
    # The root distribution imports nothing outside the standard library, so
    # its one declared requirement is its workspace sibling. Asserted as an
    # EQUALITY below, unlike the research side: a re-declared SDK here is the
    # regression this composition exists to prevent.
    "cortex": {"cortex-research"},
    # All four are DIRECT research dependencies of the supported arXiv ingest
    # path. `beautifulsoup4` and `lxml` are the two that used to arrive by
    # accident — bs4 through the withdrawn `cortex-investment` member and lxml
    # only through `trafilatura`'s own requirement — and `lxml` is never
    # imported by name at all: `paper_ingest.py:125` asks BeautifulSoup for the
    # "lxml" parser as a STRING, which no import scan can see.
    "cortex-research": {"beautifulsoup4", "httpx", "lxml", "sqlite-vec"},
}

#: Installed into the smoke venv before the import statement below runs. The
#: root wheel needs nothing: that empty list is the assertion, not an omission.
IMPORT_DEPENDENCIES = {
    "cortex": [],
    "cortex-research": ["beautifulsoup4", "httpx", "lxml", "sqlite-vec"],
}


# The product is composed of exactly two workspace roots. `cortex-investment`
# and `cortex-platform-memory` were the other two; neither belongs to a
# research-only product, so the root manifest, the uv workspace and
# `distribution/bundle.py`'s closure roots have to keep naming the same pair
# rather than drifting apart one file at a time.
RESEARCH_ONLY_COMPOSITION = ("cortex", "cortex-research")
WITHDRAWN_WORKSPACE_MEMBERS = {"cortex-investment", "cortex-platform-memory"}


def _build_wheel(package: str, output_dir: Path) -> Path:
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--package",
            package,
            "--out-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    wheels = list(output_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_files) == 1, metadata_files
        return BytesParser(policy=default).parsebytes(archive.read(metadata_files[0]))


def _requirement_names(wheel: Path) -> set[str]:
    metadata = _metadata(wheel)
    return {
        canonicalize_name(Requirement(value).name)
        for value in metadata.get_all("Requires-Dist", [])
    }


def _create_venv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "venv", "--python", "3.11", str(path)],
        cwd=path.parent,
        check=True,
        text=True,
        capture_output=True,
    )
    return path / "bin" / "python"


@pytest.fixture(scope="session")
def built_wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    output_root = tmp_path_factory.mktemp("wheels")
    return {
        package: _build_wheel(package, output_root / package)
        for package in EXPECTED_WHEEL_CONTENTS
    }


def test_the_workspace_declares_exactly_the_research_only_composition() -> None:
    """Root manifest, uv workspace and bundle closure roots name one pair.

    Three files decide what a bundle contains, and only the last of them fails
    loudly: a member left in `[tool.uv.workspace]` after its requirement line
    went would keep resolving into `uv.lock`, and a name left in
    `_WORKSPACE_DISTRIBUTIONS` would demand a wheel nothing builds.
    """

    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        canonicalize_name(Requirement(value).name)
        for value in manifest["project"]["dependencies"]
    }
    members = set(manifest["tool"]["uv"]["workspace"]["members"])
    sources = {canonicalize_name(name) for name in manifest["tool"]["uv"]["sources"]}

    assert _WORKSPACE_DISTRIBUTIONS == RESEARCH_ONLY_COMPOSITION
    assert members == {"profiles/research"}
    assert sources == {"cortex-research"}
    assert "cortex-research" in declared
    assert declared & WITHDRAWN_WORKSPACE_MEMBERS == set()


def test_the_cortex_wheel_requires_no_withdrawn_workspace_member(
    built_wheels: dict[str, Path],
) -> None:
    """The metadata a bundle's closure proof is actually derived from."""

    requirements = _requirement_names(built_wheels["cortex"])

    assert "cortex-research" in requirements
    assert requirements & WITHDRAWN_WORKSPACE_MEMBERS == set()


@pytest.mark.parametrize("package", ["cortex", "cortex-research"])
def test_wheel_contains_runtime_package_and_assets(
    package: str, built_wheels: dict[str, Path]
) -> None:
    wheel = built_wheels[package]

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

    assert EXPECTED_WHEEL_CONTENTS[package] <= members


def _file_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return [
            member.filename for member in archive.infolist() if not member.is_dir()
        ]


@pytest.mark.parametrize("package", ["cortex", "cortex-research"])
def test_wheel_ships_only_the_allowlisted_trees(
    package: str, built_wheels: dict[str, Path]
) -> None:
    trees = EXPECTED_WHEEL_TREES[package]
    payload = [
        member
        for member in _file_members(built_wheels[package])
        if ".dist-info/" not in member
    ]

    assert sorted(member for member in payload if not member.startswith(trees)) == []
    for tree in trees:
        assert any(member.startswith(tree) for member in payload), tree


@pytest.mark.parametrize("package", ["cortex", "cortex-research"])
def test_wheel_member_policy_rejects_nonruntime_files(
    package: str, built_wheels: dict[str, Path]
) -> None:
    violations = sorted(
        f"{label}: {member}"
        for label, matches in FORBIDDEN_MEMBER_RULES
        for member in _file_members(built_wheels[package])
        if matches(member)
    )

    assert violations == []


def test_cortex_wheel_satisfies_the_bundle_private_access_contract(
    built_wheels: dict[str, Path],
) -> None:
    """The wheel policy may not break `distribution/bundle.py`'s own contract.

    The required module list and console scripts are read from the bundle module
    rather than copied, so narrowing the wheel fails here instead of at bundle
    verification time.
    """

    wheel = built_wheels["cortex"]
    with zipfile.ZipFile(wheel) as archive:
        members = {member.filename for member in archive.infolist() if not member.is_dir()}
        entry_points_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")

    assert set(_PRIVATE_ACCESS_MODULES) <= members

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(entry_points)
    declared = dict(parser.items("console_scripts", raw=True))
    assert all(
        declared.get(name) == target for name, target in _PRIVATE_ACCESS_SCRIPTS.items()
    )


def test_clean_venv_imports_packages_without_pythonpath(
    built_wheels: dict[str, Path], tmp_path: Path
) -> None:
    venv = tmp_path / ".venv"
    subprocess.run(
        ["uv", "venv", "--python", "3.11", str(venv)],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--no-deps",
            *(str(wheel) for wheel in built_wheels.values()),
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-I",
            "-c",
            "import cortex_platform; import cortex_research",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("package", "import_statement"),
    [
        # Every console-script target the root wheel declares, imported with
        # NOTHING installed beside the wheel itself -- not even
        # `cortex-research`, which `--no-deps` leaves out. That is the whole
        # claim of the pruned root manifest, and it is stronger than the old
        # `import cortex_platform.budget_shim`: that module imported the legacy
        # dispatch chain, so it could only ever prove the chain's SDKs resolved.
        (
            "cortex",
            "import cortex_platform.product.cli, cortex_platform.product.daemon; "
            "import cortex_platform.product.runtime_update.certify_cli; "
            "import deployment.private_access.cli",
        ),
        # Metadata below checks direct declarations. The separate import smoke
        # test uses IMPORT_DEPENDENCIES, including YAML for radar_scan.
        # The parser name is exercised, not just imported: a declaration that
        # satisfies `import bs4` but not `BeautifulSoup(html, "lxml")` would
        # pass an import-only check and fail on the live ingest path.
        (
            "cortex-research",
            "import cortex_research.db, cortex_research.paper_ingest; "
            "import bs4; bs4.BeautifulSoup('<p>x</p>', 'lxml')",
        ),
    ],
)
def test_wheels_own_and_import_direct_runtime_dependencies(
    package: str,
    import_statement: str,
    built_wheels: dict[str, Path],
    tmp_path: Path,
) -> None:
    requirement_names = _requirement_names(built_wheels[package])
    expected = EXPECTED_DIRECT_RUNTIME_DEPENDENCIES[package]
    if package == "cortex":
        assert requirement_names == expected
    else:
        assert expected <= requirement_names

    python = _create_venv(tmp_path / package / ".venv")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(built_wheels[package]),
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    if IMPORT_DEPENDENCIES[package]:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *IMPORT_DEPENDENCIES[package],
            ],
            cwd=tmp_path,
            check=True,
            text=True,
            capture_output=True,
        )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [str(python), "-I", "-c", import_statement],
        cwd=tmp_path,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
