from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from distribution.wheel_closure import parse_requirements_hashes
from tools import vendor_wheelhouse as wheelhouse


def _wheel(directory: Path, name: str, version: str, *, tag: str = "py3-none-any") -> Path:
    normalized = name.replace("-", "_")
    path = directory / f"{normalized}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{normalized}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _export(entries: Sequence[tuple[str, str, str, str]]) -> str:
    """Render uv-export-shaped lines: (name, version, marker, digest)."""

    lines: list[str] = []
    for name, version, marker, digest in entries:
        suffix = f" ; {marker}" if marker else ""
        lines.append(f"{name}=={version}{suffix} \\")
        lines.append(f"    --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


class _Harness:
    """A closed stand-in for `uv` and `pip`, so the tests never touch a network."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        export: str,
        wheels: dict[str, str],
        sdists: dict[str, str] | None = None,
        build_tag: str = "py3-none-any",
    ) -> None:
        self.tmp_path = tmp_path
        self.export = export
        self.wheels = wheels
        self.sdists = sdists or {}
        self.build_tag = build_tag
        self.commands: list[list[str]] = []
        self.source = tmp_path / "index"
        self.source.mkdir(exist_ok=True)

    def __call__(self, command: Sequence[str], cwd: Path) -> str:
        parts = [str(item) for item in command]
        self.commands.append(parts)
        if "export" in parts:
            Path(parts[parts.index("-o") + 1]).write_text(self.export)
            return ""
        if "download" in parts and "--only-binary=:all:" in parts:
            destination = Path(parts[parts.index("--dest") + 1])
            if "-r" in parts:
                # `pip download -r` resolves the WHOLE file before fetching, so
                # one unavailable distribution fails all of them and nothing is
                # written. Modelling this as "write the good ones, then raise"
                # is what hid a defect that the first real run against the lock
                # found immediately: `peewee` alone failed the other 155.
                requested = Path(parts[parts.index("-r") + 1]).read_text()
                names = [
                    line.split("==")[0].strip()
                    for line in requested.splitlines()
                    if line.strip()
                ]
                unavailable = [name for name in names if name not in self.wheels]
                if unavailable:
                    raise wheelhouse.WheelhouseError(
                        f"pip failed: no matching distribution for {unavailable}"
                    )
                for name in names:
                    _wheel(destination, name, self.wheels[name])
                return ""
            name, _, _version = parts[4].partition("==")
            if name not in self.wheels:
                raise wheelhouse.WheelhouseError(
                    f"pip failed: no matching distribution for {name}"
                )
            _wheel(destination, name, self.wheels[name])
            return ""
        if "download" in parts:
            destination = Path(parts[parts.index("--dest") + 1])
            requirement = parts[4]
            name, version = requirement.split("==")
            (destination / f"{name}-{version}.tar.gz").write_bytes(
                self.sdists.get(name, f"{name} sdist".encode())
            )
            return ""
        if "wheel" in parts:
            destination = Path(parts[parts.index("--wheel-dir") + 1])
            archive = Path(next(item for item in parts if item.endswith(".tar.gz")))
            name, version = archive.name.removesuffix(".tar.gz").rsplit("-", 1)
            _wheel(destination, name, version, tag=self.build_tag)
            return ""
        raise AssertionError(f"unexpected command: {parts}")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "uv.lock").write_text("version = 1\n")
    return repository


def test_the_marker_filter_agrees_with_the_bundle_verifier(tmp_path: Path) -> None:
    """Acquisition and verification must never disagree about markers."""

    excluded = _wheel(tmp_path, "pywin32", "311")
    included = _wheel(tmp_path, "httpx", "0.28.1")
    export = _export(
        [
            ("httpx", "0.28.1", "", _digest(included)),
            ("pywin32", "311", "sys_platform == 'win32'", _digest(excluded)),
            ("scipy", "1.17.1", "python_full_version < '3.12'", "a" * 64),
        ]
    )

    text, versions = wheelhouse.filter_for_target(export)

    assert set(versions) == {"httpx"}
    assert "pywin32" not in text
    assert "scipy" not in text


def test_an_undecidable_marker_aborts(tmp_path: Path) -> None:
    export = _export([("httpx", "0.28.1", "platform_release == '24.0'", "a" * 64)])

    with pytest.raises(wheelhouse.WheelhouseError, match="unsupported requirement marker"):
        wheelhouse.filter_for_target(export)


def test_a_retained_marker_line_keeps_its_hashes(tmp_path: Path) -> None:
    """The filtered file is what binds acquisition to the lock.

    `filter_for_target` used to partition on `;` before splitting on `--hash=`,
    so every digest on a marker-bearing line landed in the marker half and was
    dropped. The retained line still looked correct — right name, right version
    — while carrying no binding at all, and `pip download` was handed that.
    Measured against the real lock, 5 of 156 survivors lost every hash.
    """

    export = _export(
        [
            ("httpx", "0.28.1", "", "a" * 64),
            ("psutil", "7.2.2", "sys_platform != 'win32'", "b" * 64),
        ]
    )

    text, versions = wheelhouse.filter_for_target(export)

    assert set(versions) == {"httpx", "psutil"}
    filtered = tmp_path / "filtered.txt"
    filtered.write_text(text)
    assert parse_requirements_hashes(filtered) == {
        "httpx": {"a" * 64},
        "psutil": {"b" * 64},
    }


def test_the_filtered_closure_survives_a_marked_duplicate_pair(tmp_path: Path) -> None:
    """The real export lists `scipy` twice under complementary markers.

    `parse_requirements_hashes` refuses a repeated name by design, so parsing
    the universal export aborted the whole tool before it acquired anything.
    Filtering first collapses the pair to the line that applies, which is the
    state that parser documents itself as expecting.
    """

    export = _export(
        [
            ("scipy", "1.17.1", "python_full_version < '3.12'", "c" * 64),
            ("scipy", "1.18.0", "python_full_version >= '3.12'", "d" * 64),
        ]
    )

    text, versions = wheelhouse.filter_for_target(export)

    assert versions == {"scipy": "1.18.0"}
    filtered = tmp_path / "filtered.txt"
    filtered.write_text(text)
    assert parse_requirements_hashes(filtered) == {"scipy": {"d" * 64}}


def test_a_forbidden_license_boundary_aborts(tmp_path: Path) -> None:
    export = _export([("backtesting", "0.6.4", "", "a" * 64)])

    with pytest.raises(wheelhouse.WheelhouseError, match="forbidden license boundary"):
        wheelhouse.filter_for_target(export)


def test_the_closure_is_cached_under_the_lock_digest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    httpx = _wheel(tmp_path, "httpx", "0.28.1")
    anyio = _wheel(tmp_path, "anyio", "4.9.0")
    export = _export(
        [("httpx", "0.28.1", "", _digest(httpx)), ("anyio", "4.9.0", "", _digest(anyio))]
    )
    harness = _Harness(tmp_path, export=export, wheels={"httpx": "0.28.1", "anyio": "4.9.0"})

    report = wheelhouse.vendor(
        repository=repository, vendor_root=tmp_path / "vendor", run=harness
    )

    expected = hashlib.sha256((repository / "uv.lock").read_bytes()).hexdigest()
    assert report["lock_sha256"] == expected
    root = tmp_path / "vendor" / expected
    assert {path.name for path in root.glob("*.whl")} == {
        "httpx-0.28.1-py3-none-any.whl",
        "anyio-4.9.0-py3-none-any.whl",
    }
    # The cached file is the TARGET closure, not the universal export.
    # `verify_bundle` requires the requirements names to equal the shipped
    # dependency-wheel names exactly, and the export carries names this target
    # excludes — so caching the export made every real bundle unverifiable.
    assert parse_requirements_hashes(root / wheelhouse.REQUIREMENTS_NAME) == {
        "httpx": {_digest(httpx)},
        "anyio": {_digest(anyio)},
    }
    # No wheel was derived here, so the honest state is the record's ABSENCE.
    # A present-but-empty `{}` claims a derivation and then names none, which
    # `distribution/bundle.py:_validate_sdist_builds` refuses on sight -- and
    # which the research-only closure would otherwise produce on every run,
    # because every distribution left in it publishes a wheel.
    assert not (root / wheelhouse.SIDE_RECORD_NAME).exists()
    assert report["built_from_sdist"] == []


def test_a_stale_sdist_record_is_removed_by_a_pure_wheel_run(tmp_path: Path) -> None:
    """The cache root is keyed by lock digest, so an old record outlives a rerun.

    Once the producer stops writing `{}`, a record left by an earlier
    acquisition would simply stay there and go on naming wheels this run did not
    build -- and `verify_bundle` would read it as provenance for them.
    """

    repository = _repository(tmp_path)
    httpx = _wheel(tmp_path, "httpx", "0.28.1")
    export = _export([("httpx", "0.28.1", "", _digest(httpx))])
    harness = _Harness(tmp_path, export=export, wheels={"httpx": "0.28.1"})
    lock_digest = hashlib.sha256((repository / "uv.lock").read_bytes()).hexdigest()
    root = tmp_path / "vendor" / lock_digest
    root.mkdir(parents=True)
    stale = root / wheelhouse.SIDE_RECORD_NAME
    stale.write_text(
        json.dumps({"peewee": {"sdist_sha256": "a" * 64, "wheel_sha256": "b" * 64}})
    )

    report = wheelhouse.vendor(
        repository=repository, vendor_root=tmp_path / "vendor", run=harness
    )

    assert report["built_from_sdist"] == []
    assert not stale.exists()


def test_a_distribution_with_no_wheel_is_built_from_its_pinned_sdist(tmp_path: Path) -> None:
    """The real lock's `peewee` publishes no wheel at all (contract §10 A14)."""

    repository = _repository(tmp_path)
    httpx = _wheel(tmp_path, "httpx", "0.28.1")
    sdist_bytes = b"peewee sdist bytes"
    export = _export(
        [
            ("httpx", "0.28.1", "", _digest(httpx)),
            ("peewee", "3.17.3", "", hashlib.sha256(sdist_bytes).hexdigest()),
        ]
    )
    harness = _Harness(
        tmp_path,
        export=export,
        wheels={"httpx": "0.28.1"},
        sdists={"peewee": sdist_bytes},
    )

    report = wheelhouse.vendor(
        repository=repository, vendor_root=tmp_path / "vendor", run=harness
    )

    assert report["built_from_sdist"] == ["peewee"]
    root = tmp_path / "vendor" / str(report["lock_sha256"])
    assert (root / "peewee-3.17.3-py3-none-any.whl").is_file()
    record = json.loads((root / wheelhouse.SIDE_RECORD_NAME).read_text())
    assert record["peewee"]["sdist_sha256"] == hashlib.sha256(sdist_bytes).hexdigest()
    assert record["peewee"]["wheel_sha256"] == _digest(
        root / "peewee-3.17.3-py3-none-any.whl"
    )


def test_a_locally_built_wheel_that_is_not_pure_python_aborts(tmp_path: Path) -> None:
    """The one artifact in the closure whose bytes this machine produces.

    A pure-Python wheel is reproducible from its sdist on any host. The moment a
    build compiles an extension, the result carries the vendor host's toolchain
    and is pinned by nothing the lock knows about, so it must stop the build
    rather than be shipped.
    """

    repository = _repository(tmp_path)
    sdist_bytes = b"peewee sdist bytes"
    export = _export(
        [
            ("httpx", "0.28.1", "", "a" * 64),
            ("peewee", "3.17.3", "", hashlib.sha256(sdist_bytes).hexdigest()),
        ]
    )
    harness = _Harness(
        tmp_path,
        export=export,
        wheels={"httpx": "0.28.1"},
        sdists={"peewee": sdist_bytes},
        build_tag="cp314-cp314-macosx_11_0_arm64",
    )

    with pytest.raises(wheelhouse.WheelhouseError, match="is not pure Python"):
        wheelhouse.vendor(repository=repository, vendor_root=tmp_path / "vendor", run=harness)

    assert not (tmp_path / "vendor").exists()


def test_an_sdist_whose_digest_the_lock_does_not_pin_aborts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    export = _export([("peewee", "3.17.3", "", "f" * 64)])
    harness = _Harness(tmp_path, export=export, wheels={}, sdists={"peewee": b"unpinned bytes"})

    with pytest.raises(wheelhouse.WheelhouseError, match="sdist digest .* is not pinned"):
        wheelhouse.vendor(repository=repository, vendor_root=tmp_path / "vendor", run=harness)

    assert not (tmp_path / "vendor").exists()


def test_a_wheel_whose_digest_the_lock_does_not_pin_aborts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    export = _export([("httpx", "0.28.1", "", "e" * 64)])
    harness = _Harness(tmp_path, export=export, wheels={"httpx": "0.28.1"})

    with pytest.raises(wheelhouse.WheelhouseError, match="is not pinned by the lock"):
        wheelhouse.vendor(repository=repository, vendor_root=tmp_path / "vendor", run=harness)

    assert not (tmp_path / "vendor").exists()


def test_an_acquired_wheel_with_no_requirement_line_aborts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    httpx = _wheel(tmp_path, "httpx", "0.28.1")
    export = _export([("httpx", "0.28.1", "", _digest(httpx))])

    class _Extra(_Harness):
        def __call__(self, command: Sequence[str], cwd: Path) -> str:
            result = super().__call__(command, cwd)
            parts = [str(item) for item in command]
            if "download" in parts and "--only-binary=:all:" in parts:
                _wheel(Path(parts[parts.index("--dest") + 1]), "smuggled", "9.9.9")
            return result

    harness = _Extra(tmp_path, export=export, wheels={"httpx": "0.28.1"})

    with pytest.raises(wheelhouse.WheelhouseError, match="does not match the target closure"):
        wheelhouse.vendor(repository=repository, vendor_root=tmp_path / "vendor", run=harness)


def test_the_export_never_suppresses_hashes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    httpx = _wheel(tmp_path, "httpx", "0.28.1")
    export = _export([("httpx", "0.28.1", "", _digest(httpx))])
    harness = _Harness(tmp_path, export=export, wheels={"httpx": "0.28.1"})

    wheelhouse.vendor(repository=repository, vendor_root=tmp_path / "vendor", run=harness)

    exported = next(command for command in harness.commands if "export" in command)
    assert "--no-hashes" not in exported
    assert not any(item.startswith("--extra") for item in exported)
    assert "--frozen" in exported and "--offline" in exported
    assert "--no-emit-workspace" in exported


def test_two_requirement_lines_for_one_distribution_abort(tmp_path: Path) -> None:
    export = _export(
        [("scipy", "1.17.1", "", "a" * 64), ("scipy", "1.18.0", "", "b" * 64)]
    )

    with pytest.raises(wheelhouse.WheelhouseError, match="duplicate requirement"):
        wheelhouse.filter_for_target(export)
