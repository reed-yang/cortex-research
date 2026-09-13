"""Vendor, normalize, and pin the embedded CPython runtime archive.

Operator-run, once per pin. This is the only network step of the embedded
runtime contract; the bundle build itself consumes the committed pin and the
vendored archive offline. The tool is deliberately outside ``distribution/``
and is never copied into a bundle.

Contract: ``docs/plans/2026-07-29-deploy-compat-embedded-python.md`` §2, as
amended by §10 (A1 patch set and residue rules, A2 prune globs, A3 attestation
matching on digest).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


class VendorError(RuntimeError):
    """The pinned Python runtime could not be vendored safely."""


UPSTREAM_PROJECT = "astral-sh/python-build-standalone"

PYTHON_IMPLEMENTATION = "CPython"

NORMALIZER_VERSION = 1
PREFIX_MARKER = "@CORTEX_PYTHON_PREFIX@"
BUILD_MARKER = "@CORTEX_PYTHON_BUILD@"

VENDOR_RELATIVE = "vendor/python-runtime"

DOWNLOAD_LIMIT = 128 * 1024 * 1024
DOWNLOAD_TIMEOUT = 300

LICENCE_DOCUMENTS = (
    "LICENSE.bdb.txt",
    "LICENSE.bzip2.txt",
    "LICENSE.cpython.txt",
    "LICENSE.expat.txt",
    "LICENSE.libedit.txt",
    "LICENSE.libffi.txt",
    "LICENSE.liblzma.txt",
    "LICENSE.libuuid.txt",
    "LICENSE.mpdecimal.txt",
    "LICENSE.ncurses.txt",
    "LICENSE.openssl-3.txt",
    "LICENSE.sqlite.txt",
    "LICENSE.tcl.txt",
    "LICENSE.tix.txt",
    "LICENSE.zlib.txt",
    "python-licenses.rst",
)
LICENCE_RELATIVE = "share/licenses/python-build-standalone"


@dataclass(frozen=True)
class UpstreamAsset:
    """One measured python-build-standalone release asset.

    Every field is a measurement of a published artifact, so a profile that has
    not been vendored yet carries no asset at all rather than a placeholder.
    """

    release_tag: str
    name: str
    sha256: str
    size: int
    version: str


@dataclass(frozen=True)
class RuntimeProfile:
    """Everything one CPython release contributes to the vendoring policy.

    This tool used to be single-version by construction; the profile is that
    construction made explicit, so a second release is vendored by the same code
    under the same attestation and pin discipline. Every path table is derived
    from `release`, which is what makes a new profile a declaration rather than
    a transcription.
    """

    name: str
    release: str
    abi_tag: str
    platform_tag: str
    interpreter_relative: str
    library_relative: str
    library_id: str
    prune_policy: tuple[str, ...]
    patch_set: tuple[str, ...]
    residual_build_path_allowlist: frozenset[str]
    required_files: tuple[str, ...]
    required_directories: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    upstream: UpstreamAsset | None

    def pinned_upstream(self) -> UpstreamAsset:
        if self.upstream is None:
            raise VendorError(f"the {self.name} profile has no pinned upstream asset")
        return self.upstream

    @property
    def version(self) -> str:
        return self.pinned_upstream().version

    @property
    def archive_name(self) -> str:
        return f"cpython-{self.version}-{self.abi_tag}-{self.platform_tag}.tar.gz"

    @property
    def pin_name(self) -> str:
        return f"cpython-{self.version}-{self.abi_tag}-{self.platform_tag}.pin.json"


def _darwin_runtime_profile(
    *,
    release: str,
    platform_tag: str,
    measured_residual_build_paths: tuple[str, ...],
    upstream: UpstreamAsset | None,
) -> RuntimeProfile:
    """Derive one profile's whole path policy from its CPython release."""

    tag = f"cp{release.replace('.', '')}"
    library = f"lib/libpython{release}.dylib"
    return RuntimeProfile(
        name=tag,
        release=release,
        abi_tag=tag,
        platform_tag=platform_tag,
        interpreter_relative=f"bin/python{release}",
        library_relative=library,
        library_id=f"@rpath/libpython{release}.dylib",
        # Frozen prune policy. `lib/tcl9*/` and `lib/tk9*/` are the §10 A2
        # correction of the contract's `lib/tcl9/` and `lib/tk9/`, which do not
        # match the versioned directory names the artifact actually ships.
        prune_policy=(
            f"lib/python{release}/idlelib/",
            f"lib/python{release}/tkinter/",
            f"lib/python{release}/lib-dynload/_tkinter*.so",
            f"lib/python{release}/lib-dynload/_dbm*.so",
            "lib/libtcl9*.dylib",
            "lib/libtcl9tk9.0.dylib",
            "lib/tcl9*/",
            "lib/tk9*/",
            "lib/itcl*/",
            "lib/thread*/",
            "**/__pycache__/",
            "**/*.a",
        ),
        # The files from which the runtime, or a build against it, derives
        # paths. Only these are rewritten, and only these carry the strict
        # no-residue assertion.
        patch_set=(
            f"lib/python{release}/_sysconfigdata_*.py",
            f"lib/python{release}/_sysconfig_vars_*.json",
            "lib/pkgconfig/*.pc",
            f"lib/python{release}/config-{release}-darwin/Makefile",
        ),
        # Measured residual build-host paths that cannot be removed without
        # breaking something. A new holder in a future pin aborts the vendor
        # run, which is how a profile's measured set is established.
        residual_build_path_allowlist=frozenset(
            {
                f"lib/python{release}/config-{release}-darwin/python.o",
                *measured_residual_build_paths,
            }
        ),
        required_files=(
            f"bin/python{release}",
            library,
            f"lib/python{release}/LICENSE.txt",
            f"lib/python{release}/ensurepip/__init__.py",
            f"lib/python{release}/os.py",
            f"lib/python{release}/venv/__init__.py",
            f"{LICENCE_RELATIVE}/LICENSE.openssl-3.txt",
            f"{LICENCE_RELATIVE}/python-licenses.rst",
        ),
        required_directories=(
            "include",
            "lib/pkgconfig",
            f"lib/python{release}/config-{release}-darwin",
            f"lib/python{release}/site-packages/pip",
        ),
        forbidden_paths=(
            "lib/libtcl9.0.dylib",
            f"lib/python{release}/idlelib",
            f"lib/python{release}/tkinter",
        ),
        upstream=upstream,
    )


CP314_PROFILE = _darwin_runtime_profile(
    release="3.14",
    platform_tag="macosx_11_0_arm64",
    measured_residual_build_paths=(
        "lib/python3.14/site-packages/pip-26.1.2.dist-info/direct_url.json",
    ),
    upstream=UpstreamAsset(
        release_tag="20260728",
        name="cpython-3.14.6+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz",
        sha256="f4b47659e2da4b97f38cefdf5ad19f0042946099d843cde60de308708e5b1ac5",
        size=26022203,
        version="3.14.6",
    ),
)
# The per-slot worker interpreter of the S3.2 contract. Its path policy is
# complete; its upstream asset is measured the first time it is vendored, and
# until then `vendor()` refuses the profile rather than carrying a placeholder.
CP311_PROFILE = _darwin_runtime_profile(
    release="3.11",
    platform_tag="macosx_11_0_arm64",
    measured_residual_build_paths=(
        "lib/python3.11/site-packages/pip-26.1.2.dist-info/direct_url.json",
        "lib/python3.11/site-packages/setuptools-82.0.1.dist-info/direct_url.json",
    ),
    upstream=UpstreamAsset(
        release_tag="20260728",
        name="cpython-3.11.15+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz",
        sha256="3f1839e06c8a0800ac31b35d1a633323dda11d34ad8f2ed9d70cc51c56793028",
        size=27113247,
        version="3.11.15",
    ),
)
DEFAULT_PROFILE = CP314_PROFILE
PROFILES: Mapping[str, RuntimeProfile] = {
    profile.name: profile for profile in (CP314_PROFILE, CP311_PROFILE)
}

# The default profile's values under the names this tool has always published.
UPSTREAM_RELEASE_TAG = DEFAULT_PROFILE.pinned_upstream().release_tag
UPSTREAM_ASSET = DEFAULT_PROFILE.pinned_upstream().name
UPSTREAM_ASSET_SHA256 = DEFAULT_PROFILE.pinned_upstream().sha256
UPSTREAM_ASSET_SIZE = DEFAULT_PROFILE.pinned_upstream().size
PYTHON_VERSION = DEFAULT_PROFILE.version
PYTHON_RELEASE = DEFAULT_PROFILE.release
ABI_TAG = DEFAULT_PROFILE.abi_tag
PLATFORM_TAG = DEFAULT_PROFILE.platform_tag
INTERPRETER_RELATIVE = DEFAULT_PROFILE.interpreter_relative
ARCHIVE_NAME = DEFAULT_PROFILE.archive_name
PIN_NAME = DEFAULT_PROFILE.pin_name
PRUNE_POLICY = DEFAULT_PROFILE.prune_policy
PATCH_SET = DEFAULT_PROFILE.patch_set
RESIDUAL_BUILD_PATH_ALLOWLIST = DEFAULT_PROFILE.residual_build_path_allowlist
REQUIRED_FILES = DEFAULT_PROFILE.required_files
REQUIRED_DIRECTORIES = DEFAULT_PROFILE.required_directories
FORBIDDEN_PATHS = DEFAULT_PROFILE.forbidden_paths
LIBPYTHON_RELATIVE = DEFAULT_PROFILE.library_relative
LIBPYTHON_ID = DEFAULT_PROFILE.library_id

_MACH_O_MAGIC = frozenset(
    {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTALL_PATH = re.compile(r"/install(?![A-Za-z0-9_.-])")
_BUILD_TEMPORARY_ROOT = re.compile(
    r"(?:/private)?/var/folders/[^/\s\"'\\]+/[^/\s\"'\\]+/T/[^/\s\"'\\]+"
)
_MAPPING_CONFIG_ARGS = re.compile(r'("CONFIG_ARGS"\s*:\s*)"(?:[^"\\]|\\.)*"')
_MAKE_CONFIG_ARGS = re.compile(r"^(CONFIG_ARGS=).*$", re.MULTILINE)

CommandRunner = Callable[..., str]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _default_runner(*command: str) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={"HOME": "", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VendorError(f"{command[0]} could not be run") from exc
    if completed.returncode != 0:
        raise VendorError(f"{command[0]} failed: {completed.stderr.strip()[:200]}")
    return completed.stdout


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def fetch(url: str) -> bytes:
    """Download one bounded resource. The only network primitive in this tool."""

    request = urllib.request.Request(url, headers={"User-Agent": "cortex-vendor-python"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            payload = response.read(DOWNLOAD_LIMIT + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VendorError(f"could not download {url}") from exc
    if len(payload) > DOWNLOAD_LIMIT:
        raise VendorError(f"{url} exceeds the download limit")
    return payload


def verify_asset(
    asset: Path,
    *,
    sha256sums: str,
    profile: RuntimeProfile = DEFAULT_PROFILE,
) -> str:
    """Bind the downloaded asset to two independent pins and return its digest."""

    upstream = profile.pinned_upstream()
    listed: str | None = None
    for line in sha256sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == upstream.name:
            listed = parts[0]
            break
    if listed is None or not _SHA256.fullmatch(listed):
        raise VendorError("the release SHA256SUMS does not list the pinned asset")
    payload = asset.read_bytes()
    observed = hashlib.sha256(payload).hexdigest()
    if observed != listed:
        raise VendorError("asset digest does not match the release SHA256SUMS entry")
    if observed != upstream.sha256:
        raise VendorError("asset digest does not match the digest pinned in this tool")
    if len(payload) != upstream.size:
        raise VendorError("asset size does not match the pinned size")
    return observed


def select_attestations(document: bytes, *, digest: str) -> bytes:
    """Keep every attestation bundle that binds `digest`, requiring one in Rekor.

    Matching is on the digest, never on the release asset name: the pinned
    release publishes the Rekor-backed bundle under the artifact's *build* name
    and the release name only under a bundle with no transparency-log entry
    (contract §10 A3).
    """

    try:
        payload = json.loads(document)
    except json.JSONDecodeError as exc:
        raise VendorError("the attestation response is not JSON") from exc
    attestations = payload.get("attestations") if isinstance(payload, dict) else None
    if not isinstance(attestations, list) or not attestations:
        raise VendorError("the attestation response carries no attestations")
    binding: list[object] = []
    rekor_backed = False
    for entry in attestations:
        if not isinstance(entry, dict):
            raise VendorError("the attestation response is malformed")
        bundle = entry.get("bundle")
        if not isinstance(bundle, dict):
            raise VendorError("the attestation response is malformed")
        envelope = bundle.get("dsseEnvelope")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), str):
            raise VendorError("the attestation response is malformed")
        try:
            statement = json.loads(base64.b64decode(envelope["payload"], validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            raise VendorError("an attestation statement is unreadable") from exc
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(subjects, list):
            raise VendorError("an attestation statement carries no subject")
        bound = any(
            isinstance(subject, dict)
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == digest
            for subject in subjects
        )
        if not bound:
            continue
        binding.append(bundle)
        material = bundle.get("verificationMaterial")
        entries = material.get("tlogEntries") if isinstance(material, dict) else None
        if isinstance(entries, list) and entries:
            rekor_backed = True
    if not binding:
        raise VendorError("no attestation binds the asset digest")
    if not rekor_backed:
        raise VendorError("no Rekor-backed attestation binds the asset digest")
    return _canonical_json_bytes({"digest": digest, "bundles": binding})


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _prune_matches(
    relative: str, name: str, *, directory: bool, policy: tuple[str, ...]
) -> bool:
    for pattern in policy:
        if pattern.startswith("**/"):
            tail = pattern[3:]
            if tail.endswith("/"):
                if directory and fnmatch(name, tail[:-1]):
                    return True
            elif not directory and fnmatch(name, tail):
                return True
        elif pattern.endswith("/"):
            if directory and fnmatch(relative, pattern[:-1]):
                return True
        elif not directory and fnmatch(relative, pattern):
            return True
    return False


def prune_tree(tree: Path, *, profile: RuntimeProfile = DEFAULT_PROFILE) -> tuple[str, ...]:
    """Remove every path the frozen policy forbids and report what went."""

    policy = profile.prune_policy
    removed: list[str] = []
    for current, directories, files in os.walk(tree, topdown=True):
        base = Path(current)
        retained: list[str] = []
        for name in sorted(directories):
            relative = (base / name).relative_to(tree).as_posix()
            if _prune_matches(relative, name, directory=True, policy=policy):
                shutil.rmtree(base / name)
                removed.append(relative)
            else:
                retained.append(name)
        directories[:] = retained
        for name in sorted(files):
            relative = (base / name).relative_to(tree).as_posix()
            if _prune_matches(relative, name, directory=False, policy=policy):
                (base / name).unlink()
                removed.append(relative)
    return tuple(sorted(removed))


def materialize_symlinks(tree: Path) -> tuple[str, ...]:
    """Replace every symlink with a regular copy of its in-tree target.

    `verify_bundle` refuses a bundle carrying a symlink, and the staged
    generation's component checks refuse anything that is not a regular file,
    so the shipped archive must contain directories and regular files only.
    """

    root = tree.resolve()
    materialized: list[str] = []
    for path in sorted(tree.rglob("*")):
        if not path.is_symlink():
            continue
        relative = path.relative_to(tree).as_posix()
        try:
            target = path.resolve(strict=True)
        except OSError as exc:
            raise VendorError(f"{relative} is a symlink that does not resolve") from exc
        if not target.is_relative_to(root) or not target.is_file():
            raise VendorError(f"{relative} is a symlink outside the runtime tree")
        payload = target.read_bytes()
        mode = stat.S_IMODE(target.stat().st_mode)
        path.unlink()
        path.write_bytes(payload)
        path.chmod(mode)
        materialized.append(relative)
    return tuple(materialized)


def _patch_set_members(tree: Path, *, profile: RuntimeProfile) -> tuple[str, ...]:
    members: set[str] = set()
    for pattern in profile.patch_set:
        for path in tree.glob(pattern):
            if path.is_file() and not path.is_symlink():
                members.add(path.relative_to(tree).as_posix())
    return tuple(sorted(members))


def patch_build_host_paths(
    tree: Path, *, profile: RuntimeProfile = DEFAULT_PROFILE
) -> tuple[str, ...]:
    """Rewrite every build-host path in the patch set and report the members.

    `/install` becomes the prefix marker, substituted with the real generation
    path at stage time. The build host's temporary root becomes an inert marker
    that is never substituted — it exists so the shipped artifact carries no
    identifying build-host layout and is deterministic across build hosts.
    """

    members = _patch_set_members(tree, profile=profile)
    for relative in members:
        path = tree / relative
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise VendorError(f"{relative} is not decodable text") from exc
        if relative.endswith("Makefile"):
            text = _MAKE_CONFIG_ARGS.sub(r"\1", text)
        else:
            text = _MAPPING_CONFIG_ARGS.sub(r'\1""', text)
        text = _INSTALL_PATH.sub(PREFIX_MARKER, text)
        text = _BUILD_TEMPORARY_ROOT.sub(BUILD_MARKER, text)
        path.write_text(text, encoding="utf-8")
    return members


def assert_no_residual_build_paths(
    tree: Path, *, profile: RuntimeProfile = DEFAULT_PROFILE
) -> None:
    """Fail closed on any build-host path the normalization did not neutralize.

    Scoped deliberately (contract §10 A1): `/install` is asserted only inside
    the patch set, because the interpreter binary embeds it as frozen
    `getpath.py`'s compile-time `PREFIX` constant and cannot be rid of it. The
    behavioural guarantee is delivered instead by the staged probe, which
    requires the runtime's own `BINDIR`/`LIBDIR`/`INCLUDEPY` to name the
    generation.
    """

    patched = set(_patch_set_members(tree, profile=profile))
    allowlist = profile.residual_build_path_allowlist
    for path in sorted(tree.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(tree).as_posix()
        payload = path.read_bytes()
        if relative in patched and (
            _INSTALL_PATH.search(payload.decode("utf-8", "replace"))
            or b"/var/folders" in payload
        ):
            raise VendorError(f"{relative} retains a build-host path after patching")
        if b"/var/folders" in payload and relative not in allowlist:
            raise VendorError(f"{relative} retains a build-host path")


def normalize_dylib_id(
    tree: Path,
    *,
    run: CommandRunner = _default_runner,
    profile: RuntimeProfile = DEFAULT_PROFILE,
) -> None:
    """Require `libpython`'s install name to be relocatable, repairing it once."""

    library = tree / profile.library_relative
    if not library.is_file():
        raise VendorError(f"{profile.library_relative} is missing")
    if _dylib_id(library, run=run) == profile.library_id:
        return
    run("/usr/bin/install_name_tool", "-id", profile.library_id, str(library))
    if _dylib_id(library, run=run) != profile.library_id:
        raise VendorError("libpython dylib id could not be made relocatable")


def _dylib_id(library: Path, *, run: CommandRunner) -> str:
    report = [line.strip() for line in run("/usr/bin/otool", "-D", str(library)).splitlines()]
    populated = [line for line in report if line and not line.endswith(":")]
    if len(populated) != 1:
        raise VendorError("the libpython dylib id report is invalid")
    return populated[0]


def install_licence_documents(tree: Path, documents: Mapping[str, bytes]) -> None:
    """Vendor the upstream licence chain the stripped tarball omits."""

    if set(documents) != set(LICENCE_DOCUMENTS):
        raise VendorError("the vendored licence set does not match the policy")
    root = tree / LICENCE_RELATIVE
    root.mkdir(parents=True, exist_ok=True)
    for name in sorted(documents):
        if not documents[name]:
            raise VendorError(f"licence document {name} is empty")
        (root / name).write_bytes(documents[name])


def assert_normalized_content(
    tree: Path, *, profile: RuntimeProfile = DEFAULT_PROFILE
) -> None:
    """Require the exact post-normalization shape the staging policy expects."""

    for relative in profile.required_files:
        path = tree / relative
        if not path.is_file() or path.is_symlink():
            raise VendorError(f"normalized runtime content is missing {relative}")
    for relative in profile.required_directories:
        if not (tree / relative).is_dir():
            raise VendorError(f"normalized runtime content is missing {relative}/")
    for relative in profile.forbidden_paths:
        if (tree / relative).exists():
            raise VendorError(f"normalized runtime content still carries {relative}")


def normalize_tree(
    tree: Path,
    *,
    licences: Mapping[str, bytes],
    run: CommandRunner = _default_runner,
    profile: RuntimeProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    """Apply the whole normalization policy in order and report what it did."""

    removed = prune_tree(tree, profile=profile)
    materialized = materialize_symlinks(tree)
    patched = patch_build_host_paths(tree, profile=profile)
    normalize_dylib_id(tree, run=run, profile=profile)
    install_licence_documents(tree, licences)
    assert_no_residual_build_paths(tree, profile=profile)
    assert_normalized_content(tree, profile=profile)
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "sysconfig_prefix_patched": True,
        "pruned": list(profile.prune_policy),
        "removed_count": len(removed),
        "materialized_count": len(materialized),
        "patched": list(patched),
    }


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def _is_executable_payload(payload: bytes) -> bool:
    return payload[:4] in _MACH_O_MAGIC or payload[:2] == b"#!"


def emit_archive(tree: Path, destination: Path) -> dict[str, object]:
    """Write the shipped archive deterministically and describe it from bytes."""

    entries: list[tuple[str, Path, bool]] = []
    for path in tree.rglob("*"):
        relative = path.relative_to(tree).as_posix()
        if path.is_symlink():
            raise VendorError(f"{relative} is a symlink and cannot be shipped")
        if path.is_dir():
            entries.append((relative, path, True))
        elif path.is_file():
            entries.append((relative, path, False))
        else:
            raise VendorError(f"{relative} is not a directory or a regular file")
    entries.sort(key=lambda entry: entry[0])
    with destination.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for relative, path, directory in entries:
                    info = tarfile.TarInfo(relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if directory:
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.size = 0
                        archive.addfile(info)
                        continue
                    payload = path.read_bytes()
                    info.type = tarfile.REGTYPE
                    info.mode = 0o755 if _is_executable_payload(payload) else 0o644
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
    emitted = destination.read_bytes()
    return {
        "name": destination.name,
        "size": len(emitted),
        "sha256": hashlib.sha256(emitted).hexdigest(),
    }


def build_pin(
    *,
    archive: Mapping[str, object],
    attestation_sha256: str,
    profile: RuntimeProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    """Return the committed pin descriptor for the normalized archive."""

    upstream = profile.pinned_upstream()
    if not _SHA256.fullmatch(attestation_sha256):
        raise VendorError("the attestation digest is invalid")
    return {
        "schema_version": 1,
        "implementation": PYTHON_IMPLEMENTATION,
        "version": profile.version,
        "abi_tag": profile.abi_tag,
        "platform_tag": profile.platform_tag,
        "interpreter_path": profile.interpreter_relative,
        "upstream": {
            "project": UPSTREAM_PROJECT,
            "release_tag": upstream.release_tag,
            "asset": upstream.name,
            "sha256": upstream.sha256,
            "attestation_sha256": attestation_sha256,
        },
        "normalization": {
            "normalizer_version": NORMALIZER_VERSION,
            "sysconfig_prefix_patched": True,
            "pruned": list(profile.prune_policy),
        },
        "archive": {
            "name": str(archive["name"]),
            "size": int(archive["size"]),  # type: ignore[arg-type]
            "sha256": str(archive["sha256"]),
        },
    }


# --------------------------------------------------------------------------
# Operator entry point
# --------------------------------------------------------------------------


def _extract_upstream(asset: Path, into: Path) -> Path:
    with tarfile.open(asset, "r:gz") as archive:
        archive.extractall(into, filter="data")
    roots = [path for path in into.iterdir()]
    if len(roots) != 1 or not roots[0].is_dir():
        raise VendorError("the upstream asset does not carry a single root directory")
    return roots[0]


def _release_url(name: str, *, release_tag: str) -> str:
    quoted = name.replace("+", "%2B")
    return (
        f"https://github.com/{UPSTREAM_PROJECT}/releases/download/"
        f"{release_tag}/{quoted}"
    )


def _licence_url(name: str, *, release_tag: str) -> str:
    return f"https://raw.githubusercontent.com/{UPSTREAM_PROJECT}/{release_tag}/{name}"


def _attestation_url(digest: str) -> str:
    return f"https://api.github.com/repos/{UPSTREAM_PROJECT}/attestations/sha256:{digest}"


def vendor(
    *,
    vendor_root: Path,
    asset: Path | None = None,
    download: Callable[[str], bytes] = fetch,
    run: CommandRunner = _default_runner,
    profile: RuntimeProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    """Acquire, normalize, emit, and pin the runtime. Writes nothing on failure."""

    # An unpinned profile is refused before anything is downloaded: the two
    # independent pins are the whole point of the acquisition step.
    upstream = profile.pinned_upstream()
    archive_name = profile.archive_name
    with tempfile.TemporaryDirectory(prefix="cortex-python-runtime-") as scratch:
        workspace = Path(scratch)
        if asset is None:
            payload = download(_release_url(upstream.name, release_tag=upstream.release_tag))
            asset = workspace / upstream.name
            asset.write_bytes(payload)
        sums = download(
            _release_url("SHA256SUMS", release_tag=upstream.release_tag)
        ).decode("utf-8", "replace")
        digest = verify_asset(asset, sha256sums=sums, profile=profile)
        attestation = select_attestations(download(_attestation_url(digest)), digest=digest)
        licences = {
            name: download(_licence_url(name, release_tag=upstream.release_tag))
            for name in LICENCE_DOCUMENTS
        }

        extracted = workspace / "extracted"
        extracted.mkdir()
        tree = _extract_upstream(asset, extracted)
        normalize_tree(tree, licences=licences, run=run, profile=profile)

        staged = workspace / archive_name
        described = emit_archive(tree, staged)
        replay = workspace / f"replay-{archive_name}"
        emit_archive(tree, replay)
        if replay.read_bytes() != staged.read_bytes():
            raise VendorError("the normalized archive is not deterministic")

        pin = build_pin(
            archive=described,
            attestation_sha256=hashlib.sha256(attestation).hexdigest(),
            profile=profile,
        )
        vendor_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, vendor_root / archive_name)
        (vendor_root / f"{archive_name}.attestation.json").write_bytes(attestation)
        (vendor_root / profile.pin_name).write_bytes(_canonical_json_bytes(pin) + b"\n")
    return pin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendor-root",
        type=Path,
        default=REPOSITORY / VENDOR_RELATIVE,
        help="directory that receives the archive, attestation, and pin",
    )
    parser.add_argument(
        "--asset",
        type=Path,
        default=None,
        help="already-downloaded upstream asset; still verified against both pins",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE.name,
        help="CPython runtime profile to vendor",
    )
    arguments = parser.parse_args(argv)
    try:
        pin = vendor(
            vendor_root=arguments.vendor_root,
            asset=arguments.asset,
            profile=PROFILES[arguments.profile],
        )
    except VendorError as exc:
        print(f"vendor-python-runtime: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(pin, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
