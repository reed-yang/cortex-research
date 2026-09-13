"""Build and verify closed unsigned Cortex developer bundles."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .capabilities import artifact_capabilities
from .web_fonts import (
    KATEX_ASSET, KATEX_LICENSE, KATEX_MANIFEST, WebFontError, katex_supply, validate_fonts,
)
from .wheel_closure import (
    ClosureEntry,
    WheelClosureError,
    normalize_name,
    parse_requirements_hashes,
    prove_closure,
    read_wheel_metadata,
    satisfies,
    wheel_tags_compatible,
)
from .schema import canonical_json_bytes

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WEB_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
_RELEASE_BUILD_MARKER = re.compile(r"^cortex-r0-build-[A-Za-z0-9._-]+$")
_NODE_ADAPTER_VERSION = 1
# The Web payload JavaScript is analysed by a real ECMAScript parser. A
# hand-written tokenizer cannot decide the regex-versus-division ambiguity
# soundly, which let a payload hide an executed import inside a literal.
# The digest is pinned here, in source, so a bundle-resident copy of the parser
# can never be substituted. See docs/plans/2026-07-29-web-closure-acorn-gate.md.
_ACORN_SHA256 = "efb0124a960b34d53f9928c4926bfcfd300bb6a3d7ab64ee949b3a8bed1c7e5f"
_ACORN_RELATIVE = "vendor/acorn-8.16.0.mjs"
_WEB_ANALYSER_RELATIVE = "web_closure.mjs"
# `distribution.product_manifest` imports the staging kernel out of the wheel's
# `cortex_platform`, but `cortex-dist` runs under a host interpreter that has no
# installed product, so the kernel travels with the tools like the Web analyser
# does. Its package marker EXTENDS `__path__` rather than declaring a package:
# an installed generation puts `tools/` first on `sys.path`, and a plain
# `__init__.py` there would shadow the generation's own `cortex_platform` and
# hide every other submodule of it.
_TOOLS_PLATFORM_PACKAGE = "cortex_platform"
_TOOLS_RUNTIME_STAGING_RELATIVE = f"{_TOOLS_PLATFORM_PACKAGE}/runtime_staging.py"
_TOOLS_PLATFORM_MARKER = (
    "import pkgutil\n"
    "\n"
    "__path__ = pkgutil.extend_path(__path__, __name__)\n"
)
_WEB_ANALYSER_TIMEOUT = 180
_WEB_ANALYSER_OUTPUT_LIMIT = 64 * 1024 * 1024
# The largest module in the shipped payload is ~282 KiB. This cap is generous for
# any real build and stops a hostile bundle from making every verification cost
# gigabytes of request construction, including inside a deadline-bounded
# lifecycle call.
_WEB_SOURCE_SIZE_LIMIT = 8 * 1024 * 1024
# The bundle's entrypoints, defined once so composition writes and verification
# expects exactly the same bytes.
# §9.1's floor, now actually enforced. The tooling runs under whatever
# `${PYTHON:-python3}` resolves to, so without this an operator on macOS's stock
# 3.9.6 gets a raw `TypeError` naming a file inside the bundle.
#
# 3.9 was attempted and abandoned on evidence. Three independent static
# checks — a hand-written AST scan, a second reviewer's scan, and `vermin` —
# all concluded "3.9", and all three missed `Path.stat(follow_symlinks=False)`
# in the staging path, which is 3.10+ and which a single real run found
# immediately. Establishing a floor statically is not sound, nothing runs the
# suite under the floor, and the "no toolchain needed" prize is illusory anyway:
# verification already requires an explicit `--node-executable` with no PATH
# discovery, so an operator has a real toolchain regardless.
#
# What §3 actually claims is that the host Python need not MATCH the bundle's,
# and 3.11/3.13 against an embedded 3.14 prove exactly that.
#
# This prelude must stay parseable and runnable BELOW the floor — its whole job
# is to say so in one line instead of failing later, elsewhere, in a traceback.
_TOOLS_ENTRYPOINT = (
    "import sys\n"
    "if sys.version_info < (3, 11):\n"
    "    sys.exit(\n"
    "        'cortex-dist requires Python 3.11 or newer, but this interpreter is '\n"
    "        + '.'.join(str(part) for part in sys.version_info[:3])\n"
    "        + '. Re-run with PYTHON=/path/to/python3.11-or-newer.'\n"
    "    )\n"
    "from pathlib import Path\n"
    "sys.dont_write_bytecode = True\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
    "from distribution.cli import main\n"
    "raise SystemExit(main())\n"
)
_TOOLS_LAUNCHER = (
    "#!/bin/sh\n"
    "set -eu\n"
    'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
    'PYTHONDONTWRITEBYTECODE=1 exec "${PYTHON:-python3}" "$SCRIPT_DIR/tools/cortex_dist.py" "$@"\n'
)
# In-memory, content-keyed analysis cache. Never persisted: a stored verdict
# would be a forgeable substitute for parsing the bytes.
_WEB_ANALYSIS_CACHE: dict[object, dict[str, dict[str, object]]] = {}
_MANIFEST_FIELDS_V1 = {
    "schema_version",
    "release_id",
    "release_sequence",
    "channel",
    "created_at",
    "source",
    "target",
    "python",
    "artifacts",
    "dependency_closure",
    "capabilities",
    "hermes_boundary",
    "signing",
}
_MANIFEST_FIELDS_V2 = _MANIFEST_FIELDS_V1 | {"web_payload", "private_access"}
_MANIFEST_FIELDS_V3 = _MANIFEST_FIELDS_V2 | {"python_runtime"}
_MANIFEST_FIELDS_BY_SCHEMA = {
    1: _MANIFEST_FIELDS_V1,
    2: _MANIFEST_FIELDS_V2,
    3: _MANIFEST_FIELDS_V3,
}
_ARTIFACT_FIELDS = {
    "path",
    "role",
    "name",
    "version",
    "size",
    "sha256",
    "python_tag",
    "platform_tag",
}
# Schema 3 stops discarding the ABI tag, because that is the field the
# interpreter binding is decided on. Older tiers keep their closed field set.
_ARTIFACT_FIELDS_V3 = _ARTIFACT_FIELDS | {"abi_tag"}
# The roles a wheel built from the workspace carries, as opposed to one resolved
# from the index. Named separately because the recorded build commands of an
# installed generation are checked against exactly this set of artifacts.
_WORKSPACE_WHEEL_ROLES = frozenset({"cortex-wheel", "profile-wheel"})
_WHEEL_ROLES = _WORKSPACE_WHEEL_ROLES | {"dependency-wheel"}
# The workspace roots the dependency closure is proven from, and the wheels a
# schema-3 bundle must contain. The research-only product has two: the
# `cortex-investment` and `cortex-platform-memory` members left the composition
# with their code, and a name kept here would demand a wheel nothing builds.
# Roles are still derived rather than enumerated (`_WHEEL_ROLES` below), so this
# tuple is the only place the member count is written down.
_WORKSPACE_DISTRIBUTIONS = ("cortex", "cortex-research")
_BUILD_COMMAND_PREFIX = "uv build --offline --wheel --package "
_EXPORT_COMMAND = (
    "uv export --frozen --offline --no-dev --no-emit-workspace --format requirements-txt"
)
_PYTHON_RUNTIME_ROOT = "artifacts/python/"
_REQUIREMENTS_RELATIVE = "artifacts/requirements.txt"
_SDIST_BUILDS_RELATIVE = "artifacts/sdist-builds.json"
# The filename `tools/vendor_wheelhouse.py` writes beside the cached closure.
# Duplicated rather than imported: `tools/` is not importable from an installed
# generation, and this constant is part of the bundle's own on-disk contract.
_SDIST_BUILDS_NAME = "built-from-sdist.json"
_SDIST_BUILD_FIELDS = {"sdist_sha256", "wheel_sha256"}
_MAX_REQUIREMENTS_BYTES = 1024 * 1024
_MAX_SUPPORTED_MACOS_MAJOR = 26
# AGPL. Named rather than inferred, because a licence boundary must fail closed
# on a name the resolver could otherwise pull in transitively.
_FORBIDDEN_DISTRIBUTIONS = frozenset({"backtesting"})
_PYTHON_RUNTIME_FIELDS = {
    "path",
    "size",
    "sha256",
    "implementation",
    "version",
    "abi_tag",
    "platform_tag",
    "interpreter_path",
    "upstream",
    "normalization",
    "policy",
}
_PYTHON_RUNTIME_UPSTREAM_FIELDS = {
    "project",
    "release_tag",
    "asset",
    "sha256",
    "attestation_sha256",
}
_PYTHON_RUNTIME_NORMALIZATION_FIELDS = {
    "normalizer_version",
    "sysconfig_prefix_patched",
    "pruned",
}
_PYTHON_RUNTIME_POLICY = {
    "runtime": "embedded-cpython",
    "source_project": "astral-sh/python-build-standalone",
    "flavour": "install_only_stripped",
    "dynamic_dependency_policy": "system-libraries-only",
    "relocation": "executable-relative-rpath",
    "venv_mode": "copies-no-symlinks",
    "supported_platform": "darwin",
    "supported_architecture": "arm64",
    "identity_claim": "digest-bound-archive-extracted-into-immutable-generation",
    "host_interpreter_required": False,
}
_PRIVATE_ACCESS_MODULES = [
    "deployment/private_access/__init__.py",
    "deployment/private_access/cli.py",
    "deployment/private_access/gateway.py",
    "deployment/private_access/supervision.py",
]
_PRIVATE_ACCESS_SCRIPTS = {
    "cortex-private-access": "deployment.private_access.cli:main",
    "cortex-private-access-gateway": "deployment.private_access.gateway:main",
    "cortex-private-access-supervisor": "deployment.private_access.supervision:main",
}
# The Node builtins the shipped payload actually imports. A GRAMMAR (`node:` plus
# any lowercase name) would admit exactly the builtins that defeat module-closure
# analysis: `node:module` (`createRequire(...)("child_process")`), `node:vm`, and
# `node:child_process` itself. An allowlist keeps the closure meaningful; a future
# payload that needs another builtin fails closed and must extend this set.
_ALLOWED_NODE_BUILTINS = frozenset(
    {
        "node:async_hooks",
        "node:crypto",
        "node:fs",
        "node:fs/promises",
        "node:http",
        "node:path",
        "node:stream",
        "node:url",
    }
)
# `package.json` controls module RESOLUTION: dropping `"type":"module"` would make
# the server entry CommonJS, where `require(...)` is invisible to an ESM closure
# analysis. Its exact bytes are therefore pinned.
_REQUIRED_WEB_PACKAGE_JSON = b'{"private":true,"type":"module"}\n'
_NODE_POLICY = {
    "runtime": "compatible-host-node",
    "allowed_imports": ["node-builtins", "relative-payload"],
    "forbidden_commands": ["npm", "npx", "vinext", "wrangler"],
}
_REQUIRED_WEB_PATHS = {
    "THIRD_PARTY_NOTICES.md",
    "licenses/OFL-1.1.txt",
    "package.json",
    "client/manifest.webmanifest",
    "client/offline.html",
    "client/sw.js",
    "client/icons/cortex-180.png",
    "client/icons/cortex-192.png",
    "client/icons/cortex-512.png",
    "server/__vite_rsc_assets_manifest.js",
    "server/index.js",
    "server/node-adapter.mjs",
    "server/ssr/__vite_rsc_assets_manifest.js",
    "server/ssr/index.js",
}
# Allowed but deliberately NOT required. `apps/web/scripts/release-payload.mjs`
# FIRST_PARTY_FILES may gain a first-party server module in a later generation;
# the payload of every EARLIER generation lacks it. These same tools re-verify an
# older bundle during `cortex-dist upgrade` (Installer.upgrade verifies the
# currently installed version before switching) and during `cortex-dist rollback`
# (both the current and the target version are verified), so a new entry in
# `_REQUIRED_WEB_PATHS` would make every earlier bundle fail verification under
# the newer tools and close the rollback leg. Allowing without requiring keeps
# cross-generation verification open while the closure allowlist stays exact.
_OPTIONAL_WEB_PATHS = {
    "server/access-identity-bound.mjs",
    KATEX_LICENSE,
    KATEX_MANIFEST,
}
_WEB_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".txt",
    ".webmanifest",
}
_FONT_COMPONENTS = {
    "9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608": (
        "Geist Latin",
        "https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwcGFWNOITd.woff2",
    ),
    "5f3d6ad60f29d6cb708414ec6887163d63bf197377ef5417d2483ff31ace6c3b": (
        "Geist Mono Latin",
        "https://fonts.gstatic.com/s/geistmono/v6/or3nQ6H-1_WfwkMZI_qYFrcdmhHkjko.woff2",
    ),
}


class BundleVerificationError(RuntimeError):
    """The developer bundle violates its integrity or compatibility contract."""


def _katex_supply(files, prefix=""):
    try:
        return katex_supply(files, prefix)
    except WebFontError as exc:
        raise BundleVerificationError(str(exc)) from exc


@dataclass(frozen=True)
class BundleResult:
    path: Path
    digest: str


@dataclass(frozen=True)
class VerifiedBundle:
    path: Path
    digest: str
    manifest: dict[str, object]
    sbom: dict[str, object]
    provenance: dict[str, object]


def _canonical(value: object) -> bytes:
    return canonical_json_bytes(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value) + b"\n")


def _validate_web_source(root: Path, ledger: Path) -> None:
    try:
        root_details = root.lstat()
        ledger_details = ledger.lstat()
    except OSError as exc:
        raise BundleVerificationError("unsafe Web closure input") from exc
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_details.st_mode)
        or ledger.is_symlink()
        or not stat.S_ISREG(ledger_details.st_mode)
        or ledger_details.st_nlink != 1
    ):
        raise BundleVerificationError("unsafe Web closure input")
    try:
        for item in root.rglob("*"):
            details = item.lstat()
            if stat.S_ISLNK(details.st_mode):
                raise BundleVerificationError("unsafe Web closure input")
            if stat.S_ISDIR(details.st_mode):
                continue
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise BundleVerificationError("unsafe Web closure input")
    except OSError as exc:
        raise BundleVerificationError("unsafe Web closure input") from exc


def _parse_web_ledger(path: Path) -> dict[str, tuple[str, int]]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise BundleVerificationError("canonical Web payload ledger is oversized")
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BundleVerificationError("canonical Web payload ledger is unreadable") from exc
    if not text or not text.endswith("\n") or "\r" in text:
        raise BundleVerificationError("canonical Web payload ledger is malformed")
    entries: dict[str, tuple[str, int]] = {}
    paths: list[str] = []
    for line in text.removesuffix("\n").split("\n"):
        match = re.fullmatch(r"([0-9a-f]{64})  (0|[1-9][0-9]*)  (.+)", line)
        if match is None:
            raise BundleVerificationError("canonical Web payload ledger is malformed")
        digest, size, relative = match.groups()
        parts = relative.split("/")
        if (
            relative.startswith("/")
            or "\\" in relative
            or "\0" in relative
            or any(part in {"", ".", ".."} for part in parts)
            or relative in entries
        ):
            raise BundleVerificationError("canonical Web payload ledger path is unsafe")
        entries[relative] = (digest, int(size))
        paths.append(relative)
    if paths != sorted(paths):
        raise BundleVerificationError("canonical Web payload ledger is not sorted")
    return entries


def _web_path_allowed(relative: str) -> bool:
    if relative in _REQUIRED_WEB_PATHS or relative in _OPTIONAL_WEB_PATHS:
        return True
    return bool(
        KATEX_ASSET.fullmatch(relative)
        or re.fullmatch(r"client/assets/[A-Za-z0-9._/-]+\.(?:css|js)", relative)
        or re.fullmatch(r"server/ssr/assets/[A-Za-z0-9._-]+\.js", relative)
    )


def _resolved_analyser_node(node_executable: Path | None) -> Path:
    # The analyser runs under the operator-supplied Node, the same binary that
    # will execute the payload. The path must be explicit: a PATH-discovered
    # `node` could be substituted to report a clean closure for a dirty payload.
    if node_executable is None:
        raise BundleVerificationError("Web closure analysis requires a Node executable")
    candidate = Path(node_executable)
    if not candidate.is_absolute():
        raise BundleVerificationError("Web closure Node executable must be an absolute path")
    try:
        details = candidate.lstat()
    except OSError as exc:
        raise BundleVerificationError("Web closure Node executable is unusable") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise BundleVerificationError("Web closure Node executable is unusable")
    if not os.access(candidate, os.X_OK):
        raise BundleVerificationError("Web closure Node executable is unusable")
    return candidate


def _analyse_web_javascript(
    node_executable: Path | None,
    root: Path,
    relatives: list[str],
) -> dict[str, dict[str, object]]:
    """Parse the payload JavaScript with the pinned parser and return its facts."""

    node = _resolved_analyser_node(node_executable)
    tools = Path(__file__).resolve().parent
    acorn = tools / _ACORN_RELATIVE
    analyser = tools / _WEB_ANALYSER_RELATIVE
    for component in (acorn, analyser):
        if component.is_symlink() or not component.is_file():
            raise BundleVerificationError("Web closure analyser is missing")
    acorn_digest = _sha256(acorn)
    if acorn_digest != _ACORN_SHA256:
        raise BundleVerificationError("Web closure parser digest mismatch")
    # Identical bytes analysed by an identical parser give an identical answer, so
    # a lifecycle operation that revalidates its generation more than once pays
    # for the parse only on the first call. Keyed on content, never on a path
    # alone, and held in memory only — a cached verdict is never persisted.
    # Read each file exactly ONCE and analyse those same bytes. Sending the
    # source instead of a path means the digest that keys the cache is the digest
    # of what was parsed: there is no second read for a racing writer to swap,
    # and the analyser cannot be steered by a path or a symlink.
    sources: list[tuple[str, str]] = []
    digests: list[tuple[str, str]] = []
    for relative in relatives:
        try:
            payload = (root / relative).read_bytes()
            if len(payload) > _WEB_SOURCE_SIZE_LIMIT:
                raise BundleVerificationError("Web closure JavaScript is oversized")
            text = payload.decode()
        except (OSError, UnicodeDecodeError) as exc:
            raise BundleVerificationError("unsafe Web closure text") from exc
        sources.append((relative, text))
        digests.append((relative, hashlib.sha256(payload).hexdigest()))
    cache_key = (str(node), acorn_digest, tuple(digests))
    cached = _WEB_ANALYSIS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    request = json.dumps(
        {"files": [{"path": relative, "source": text} for relative, text in sources]}
    )
    # A closed environment: `NODE_OPTIONS` alone (via `--import`/`--require`) can
    # preload code that rewrites the analyser's report, so no caller environment
    # reaches this interpreter. Mirrors the closed env the Web server is given.
    environment = {
        "HOME": "",
        "LANG": "C",
        "LC_ALL": "C",
        "NODE_OPTIONS": "",
        "NODE_PATH": "",
        "NODE_V8_COVERAGE": "",
        "PATH": os.defpath,
    }
    # Both streams go through files rather than pipes: the request cannot deadlock
    # against the report, the timeout bounds the whole run rather than one read,
    # and an analyser that floods stdout costs disk instead of the verifier's
    # memory — the size is checked before a single byte is read back. stderr is
    # discarded outright; only the exit status and the report decide anything.
    try:
        with tempfile.TemporaryFile() as request_file, tempfile.TemporaryFile() as report:
            request_file.write(request.encode())
            request_file.seek(0)
            with subprocess.Popen(  # noqa: S603 - fixed argv, explicit interpreter
                [str(node), "--no-warnings", str(analyser), str(acorn)],
                stdin=request_file,
                stdout=report,
                stderr=subprocess.DEVNULL,
                env=environment,
            ) as process:
                try:
                    status = process.wait(timeout=_WEB_ANALYSER_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
            if report.seek(0, os.SEEK_END) > _WEB_ANALYSER_OUTPUT_LIMIT:
                raise BundleVerificationError("Web closure analyser output is oversized")
            report.seek(0)
            stdout = report.read()
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleVerificationError("Web closure analyser did not run") from exc
    if status != 0:
        raise BundleVerificationError("Web closure analyser failed")
    try:
        document = json.loads(stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise BundleVerificationError("Web closure analyser output is malformed") from exc
    analysed = document.get("files") if isinstance(document, dict) else None
    if not isinstance(analysed, list) or len(analysed) != len(relatives):
        raise BundleVerificationError("Web closure analyser output is malformed")
    facts: dict[str, dict[str, object]] = {}
    for expected, entry in zip(relatives, analysed, strict=True):
        if not isinstance(entry, dict) or entry.get("path") != expected:
            raise BundleVerificationError("Web closure analyser output is malformed")
        if entry.get("error") is not None:
            raise BundleVerificationError("unsafe Web closure JavaScript syntax")
        for key in ("references", "nonLiteralReferences", "stringLiterals", "adapterDeclarations"):
            if not isinstance(entry.get(key), list):
                raise BundleVerificationError("Web closure analyser output is malformed")
        facts[expected] = entry
    _WEB_ANALYSIS_CACHE[cache_key] = facts
    return facts


def _validate_module_specifier(relative: str, specifier: str, entries: object) -> None:
    if not specifier or "\\" in specifier or "?" in specifier or "#" in specifier:
        raise BundleVerificationError("unsafe Web closure module reference")
    if specifier.startswith("node:"):
        if specifier not in _ALLOWED_NODE_BUILTINS:
            raise BundleVerificationError("unsafe Web closure module reference")
        return
    if specifier.startswith("/"):
        if not relative.startswith("client/"):
            raise BundleVerificationError("unsafe Web closure module reference")
        resolved = posixpath.normpath(posixpath.join("client", specifier.removeprefix("/")))
        if not resolved.startswith("client/"):
            raise BundleVerificationError("unsafe Web closure module reference")
    elif specifier.startswith(("./", "../")):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), specifier))
        if resolved == ".." or resolved.startswith("../"):
            raise BundleVerificationError("unsafe Web closure module reference")
    else:
        raise BundleVerificationError("unsafe Web closure bare import")
    if resolved not in entries:
        raise BundleVerificationError("unsafe Web closure unresolved import")


def _validate_web_semantics(
    entries: dict[str, tuple[str, int]],
    files: dict[str, Path],
    prefix: str,
    build_id: str,
    adapter_version: int,
    root: Path,
    node_executable: Path | None,
) -> None:
    if any(not _web_path_allowed(relative) for relative in entries):
        raise BundleVerificationError("unsafe Web closure path")
    try:
        package = files[prefix + "package.json"].read_bytes()
    except (KeyError, OSError) as exc:
        raise BundleVerificationError("required Web payload package metadata is missing") from exc
    if package != _REQUIRED_WEB_PACKAGE_JSON:
        # Any other content could re-interpret the payload's module system, which
        # would move real imports outside what an ESM closure analysis can see.
        raise BundleVerificationError("unsafe Web closure package metadata")
    if not any(re.fullmatch(r"client/assets/[A-Za-z0-9._/-]+\.css", path) for path in entries):
        raise BundleVerificationError("required Web payload CSS is missing")
    if not any(re.fullmatch(r"client/assets/[A-Za-z0-9._/-]+\.js", path) for path in entries):
        raise BundleVerificationError("required Web payload JavaScript is missing")
    if not any(re.fullmatch(r"server/ssr/assets/[A-Za-z0-9._-]+\.js", path) for path in entries):
        raise BundleVerificationError("required Web payload server asset is missing")
    texts: dict[str, str] = {}
    for relative in entries:
        if Path(relative).suffix not in _WEB_TEXT_SUFFIXES:
            continue
        try:
            text = files[prefix + relative].read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise BundleVerificationError("unsafe Web closure text") from exc
        texts[relative] = text
        if re.search(r"sourceMappingURL=|\bfile://", text):
            raise BundleVerificationError("unsafe Web closure source metadata")
        if re.search(
            r"(?:^|[^A-Za-z0-9._-])"
            r"(?:/Users/|/home/|/private/tmp/|/tmp/|[A-Za-z]:\\Users\\)",
            text,
        ):
            raise BundleVerificationError("unsafe Web closure absolute path")
        if re.search(
            r"\b(?:npm|npx|pnpm|yarn)\s+(?:run\s+)?(?:dev|start)\b|"
            r"\bvinext\s+(?:dev|start)\b",
            text,
            re.IGNORECASE,
        ):
            raise BundleVerificationError("unsafe Web closure development command")
        if relative.startswith("client/") and re.search(
            r"CORTEX_(?:ACCESS_BOOTSTRAP_TOKEN|CONTROL_TOKEN|WEB_DRAFT_SECRET)|"
            r"X-Cortex-(?:Access-Bootstrap|Control-Token|Local-Proof)",
            text,
            re.IGNORECASE,
        ):
            raise BundleVerificationError("unsafe Web closure browser secret")
    javascript = sorted(
        relative for relative in entries if relative.endswith((".js", ".mjs"))
    )
    analysis = _analyse_web_javascript(node_executable, root, javascript)
    for relative in javascript:
        facts = analysis[relative]
        if facts["nonLiteralReferences"]:
            raise BundleVerificationError("unsafe Web closure ambiguous import")
        for reference in facts["references"]:
            if not isinstance(reference, dict) or not isinstance(
                reference.get("specifier"), str
            ):
                raise BundleVerificationError("Web closure analyser output is malformed")
            _validate_module_specifier(relative, reference["specifier"], entries)
    build_strings = analysis["server/index.js"]["stringLiterals"]
    if build_id not in build_strings or any(
        isinstance(value, str) and _RELEASE_BUILD_MARKER.fullmatch(value) and value != build_id
        for value in build_strings
    ):
        raise BundleVerificationError("unsafe Web closure build identifier")
    declarations = analysis["server/node-adapter.mjs"]["adapterDeclarations"]
    if declarations != [
        {
            "kind": "declarator",
            "declaration": "const",
            "value": adapter_version,
            "topLevel": True,
        }
    ]:
        raise BundleVerificationError("unsafe Web closure adapter version")
    try:
        validate_fonts(entries, files, prefix, texts, _FONT_COMPONENTS)
    except WebFontError as exc:
        raise BundleVerificationError(str(exc)) from exc


def _wheel_details(path: Path) -> tuple[str, str, str, str, str]:
    fields = path.name.removesuffix(".whl").rsplit("-", 4)
    if len(fields) != 5 or not path.name.endswith(".whl"):
        raise BundleVerificationError("artifact is not a valid wheel filename")
    distribution, version, python_tag, abi_tag, platform_tag = fields
    return distribution.replace("_", "-"), version, python_tag, abi_tag, platform_tag


def _artifact_entry(
    path: Path,
    relative: str,
    role: str,
    *,
    include_abi_tag: bool = False,
) -> dict[str, object]:
    name, version, python_tag, abi_tag, platform_tag = _wheel_details(path)
    entry = {
        "path": relative,
        "role": role,
        "name": name,
        "version": version,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "python_tag": python_tag,
        "platform_tag": platform_tag,
    }
    if include_abi_tag:
        entry["abi_tag"] = abi_tag
    return entry


def _release_pair(version: str) -> tuple[int, int]:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise BundleVerificationError("embedded Python version is invalid")
    return int(parts[0]), int(parts[1])


def _stage_python_runtime_artifact(
    temporary: Path,
    archive: Path,
    pin: Mapping[str, object],
) -> dict[str, object]:
    """Copy the vendored interpreter in and describe it from the copied bytes.

    The committed pin is a claim; the digest recorded in the manifest is
    re-derived from what actually landed in the bundle, and the two must agree
    or the build refuses to produce a bundle at all.
    """

    try:
        described = pin["archive"]
        upstream = pin["upstream"]
        normalization = pin["normalization"]
        if not isinstance(described, Mapping) or not isinstance(upstream, Mapping):
            raise TypeError("pin is malformed")
        if not isinstance(normalization, Mapping):
            raise TypeError("pin is malformed")
        name = str(described["name"])
    except (KeyError, TypeError) as exc:
        raise BundleVerificationError("vendored Python runtime pin is invalid") from exc
    if archive.is_symlink() or not archive.is_file():
        raise BundleVerificationError("vendored Python runtime is missing or unverified")
    root = temporary / "artifacts" / "python"
    root.mkdir(parents=True)
    destination = root / name
    shutil.copyfile(archive, destination)
    digest = _sha256(destination)
    if digest != str(described["sha256"]) or destination.stat().st_size != int(described["size"]):
        raise BundleVerificationError(
            "vendored Python runtime is missing or unverified; run tools/vendor_python_runtime.py"
        )
    return {
        "path": destination.relative_to(temporary).as_posix(),
        "size": destination.stat().st_size,
        "sha256": digest,
        "implementation": str(pin["implementation"]),
        "version": str(pin["version"]),
        "abi_tag": str(pin["abi_tag"]),
        "platform_tag": str(pin["platform_tag"]),
        "interpreter_path": str(pin["interpreter_path"]),
        "upstream": {key: upstream[key] for key in sorted(_PYTHON_RUNTIME_UPSTREAM_FIELDS)},
        "normalization": {
            key: normalization[key] for key in sorted(_PYTHON_RUNTIME_NORMALIZATION_FIELDS)
        },
        "policy": dict(_PYTHON_RUNTIME_POLICY),
    }


def _embedded_sbom_components(
    python_runtime: Mapping[str, object],
    temporary: Path,
) -> list[dict[str, object]]:
    upstream = python_runtime["upstream"]
    return [
        {
            "type": "application",
            "name": "cpython-embedded",
            "version": python_runtime["version"],
            "hashes": [{"alg": "SHA-256", "content": python_runtime["sha256"]}],
            "licenses": [{"license": {"id": "PSF-2.0"}}],
            "properties": [
                {"name": "cortex:upstream-project", "value": upstream["project"]},
                {"name": "cortex:upstream-release", "value": upstream["release_tag"]},
                {"name": "cortex:upstream-asset-sha256", "value": upstream["sha256"]},
            ],
        },
        {
            "type": "file",
            "name": "cortex-dependency-requirements",
            "hashes": [
                {"alg": "SHA-256", "content": _sha256(temporary / _REQUIREMENTS_RELATIVE)}
            ],
        },
    ]


def _build_commands(embedded: bool) -> list[str]:
    builds = [f"{_BUILD_COMMAND_PREFIX}{name}" for name in _WORKSPACE_DISTRIBUTIONS]
    if not embedded:
        return builds
    return [*builds, _EXPORT_COMMAND]


def _python_runtime_provenance(python_runtime: Mapping[str, object]) -> dict[str, object]:
    upstream = python_runtime["upstream"]
    normalization = python_runtime["normalization"]
    return {
        "release_tag": upstream["release_tag"],
        "asset": upstream["asset"],
        "asset_sha256": upstream["sha256"],
        "archive_sha256": python_runtime["sha256"],
        "normalizer_version": normalization["normalizer_version"],
        "pruned": normalization["pruned"],
    }


def _copy_distribution_tools(destination: Path) -> None:
    source = Path(__file__).resolve().parent
    package = destination / "tools" / "distribution"
    package.mkdir(parents=True)
    for path in sorted(source.glob("*.py")):
        shutil.copy2(path, package / path.name)
    # The Web closure analyser and its pinned parser travel with the tools: an
    # installed generation re-derives its own payload's module graph from bytes.
    (package / "vendor").mkdir()
    for relative in (_WEB_ANALYSER_RELATIVE, _ACORN_RELATIVE, "vendor/acorn-LICENSE.txt"):
        shutil.copy2(source / relative, package / relative)
    platform_package = destination / "tools" / _TOOLS_PLATFORM_PACKAGE
    platform_package.mkdir()
    (platform_package / "__init__.py").write_text(_TOOLS_PLATFORM_MARKER)
    shutil.copy2(
        source.parent / _TOOLS_RUNTIME_STAGING_RELATIVE,
        destination / "tools" / _TOOLS_RUNTIME_STAGING_RELATIVE,
    )
    helper = destination / "tools" / "cortex_dist.py"
    helper.write_text(_TOOLS_ENTRYPOINT)
    launcher = destination / "cortex-dist"
    launcher.write_text(_TOOLS_LAUNCHER)
    launcher.chmod(0o755)


class BundleBuilder:
    """Assemble already-built wheels into a movable developer artifact."""

    def __init__(self, output: Path) -> None:
        self.output = output

    def assemble(
        self,
        *,
        release_id: str,
        release_sequence: int,
        source_commit: str,
        lock_sha256: str,
        wheels: Iterable[Path],
        created_at: str,
        web_payload_root: Path | None = None,
        web_payload_ledger: Path | None = None,
        web_build_id: str | None = None,
        web_lock_sha256: str | None = None,
        node_adapter_version: int | None = None,
        node_executable: Path | None = None,
        python_runtime_archive: Path | None = None,
        python_runtime_pin: Mapping[str, object] | None = None,
        requirements: Path | None = None,
        dependency_wheels: Iterable[Path] = (),
        sdist_builds: Mapping[str, Mapping[str, str]] | None = None,
    ) -> BundleResult:
        if not _IDENTIFIER.fullmatch(release_id):
            raise BundleVerificationError("release identifier is unsafe")
        if type(release_sequence) is not int or release_sequence < 1:
            raise BundleVerificationError("release sequence is invalid")
        if not _COMMIT.fullmatch(source_commit) or not _SHA256.fullmatch(lock_sha256):
            raise BundleVerificationError("source provenance digest is invalid")
        web_inputs = (
            web_payload_root,
            web_payload_ledger,
            web_build_id,
            web_lock_sha256,
            node_adapter_version,
        )
        composed = all(value is not None for value in web_inputs)
        if any(value is not None for value in web_inputs) and not composed:
            raise BundleVerificationError("composed Web inputs must be provided together")
        if composed and (
            not _WEB_BUILD_ID.fullmatch(str(web_build_id))
            or not _SHA256.fullmatch(str(web_lock_sha256))
            or type(node_adapter_version) is not int
            or node_adapter_version != _NODE_ADAPTER_VERSION
        ):
            raise BundleVerificationError("composed Web metadata is invalid")
        if composed:
            _validate_web_source(Path(web_payload_root), Path(web_payload_ledger))
        embedded_inputs = (python_runtime_archive, python_runtime_pin, requirements)
        embedded = all(value is not None for value in embedded_inputs)
        if any(value is not None for value in embedded_inputs) and not embedded:
            raise BundleVerificationError("embedded runtime inputs must be provided together")
        dependency_paths = tuple(Path(path) for path in dependency_wheels)
        if dependency_paths and not embedded:
            raise BundleVerificationError("dependency wheels require an embedded runtime")
        if sdist_builds and not embedded:
            # Refused rather than dropped: silently discarding the record would
            # emit a bundle that says nothing about a locally derived wheel.
            raise BundleVerificationError("sdist build records require an embedded runtime")
        if embedded and not composed:
            raise BundleVerificationError("an embedded runtime requires a composed bundle")
        schema_version = 3 if embedded else (2 if composed else 1)
        wheel_paths = tuple(Path(path) for path in wheels)
        names = {_wheel_details(path)[0] for path in wheel_paths}
        required_names = set(_WORKSPACE_DISTRIBUTIONS)
        if not required_names.issubset(names):
            raise BundleVerificationError("Cortex and Cortex Research wheels are required")
        if self.output.exists() or self.output.is_symlink():
            raise BundleVerificationError("bundle output already exists")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".cortex-bundle-", dir=self.output.parent))
        try:
            wheel_dir = temporary / "artifacts" / "wheels"
            wheel_dir.mkdir(parents=True)
            entries: list[dict[str, object]] = []
            copied_wheels: list[Path] = []
            sources = [(wheel, False) for wheel in wheel_paths]
            sources.extend((wheel, True) for wheel in dependency_paths)
            for wheel, dependency in sorted(sources, key=lambda item: item[0].name):
                if wheel.is_symlink() or not wheel.is_file():
                    raise BundleVerificationError("wheel must be a regular file")
                destination = wheel_dir / wheel.name
                if destination.exists():
                    raise BundleVerificationError("duplicate wheel filename")
                shutil.copyfile(wheel, destination)
                copied_wheels.append(destination)
                if dependency:
                    role = "dependency-wheel"
                elif _wheel_details(wheel)[0] == "cortex":
                    role = "cortex-wheel"
                else:
                    role = "profile-wheel"
                entries.append(
                    _artifact_entry(
                        destination,
                        destination.relative_to(temporary).as_posix(),
                        role,
                        include_abi_tag=embedded,
                    )
                )
            _copy_distribution_tools(temporary)
            capabilities = artifact_capabilities(
                copied_wheels,
                system=platform.system(),
                machine=platform.machine(),
            )
            python_runtime = None
            if embedded:
                python_runtime = _stage_python_runtime_artifact(
                    temporary,
                    Path(python_runtime_archive),
                    python_runtime_pin,
                )
                shutil.copyfile(Path(requirements), temporary / _REQUIREMENTS_RELATIVE)
                if sdist_builds:
                    _write_json(temporary / _SDIST_BUILDS_RELATIVE, dict(sdist_builds))
            web_manifest = None
            private_access = None
            if composed:
                web_destination = temporary / "artifacts" / "web"
                shutil.copytree(Path(web_payload_root), web_destination)
                ledger_destination = temporary / "artifacts" / "web-payload.sha256"
                shutil.copyfile(Path(web_payload_ledger), ledger_destination)
                cortex_entry = next(entry for entry in entries if entry["role"] == "cortex-wheel")
                web_manifest = {
                    "root": "artifacts/web",
                    "ledger_path": "artifacts/web-payload.sha256",
                    "ledger_sha256": _sha256(ledger_destination),
                    "release_build_id": web_build_id,
                    "package_lock_sha256": web_lock_sha256,
                    "node_adapter_version": node_adapter_version,
                    "node_policy": _NODE_POLICY,
                }
                private_access = {
                    "wheel_path": cortex_entry["path"],
                    "wheel_sha256": cortex_entry["sha256"],
                    "required_modules": _PRIVATE_ACCESS_MODULES,
                    "console_scripts": _PRIVATE_ACCESS_SCRIPTS,
                }
            if embedded:
                major, minor = _release_pair(str(python_runtime["version"]))
                python_block = {
                    "implementation": "CPython",
                    "major": major,
                    "minor": minor,
                    "embedded": True,
                }
            else:
                python_block = {
                    "implementation": platform.python_implementation(),
                    "major": int(platform.python_version_tuple()[0]),
                    "minor": int(platform.python_version_tuple()[1]),
                    "embedded": False,
                }
            manifest = {
                "schema_version": schema_version,
                "release_id": release_id,
                "release_sequence": release_sequence,
                "channel": "developer-unsigned",
                "created_at": created_at,
                "source": {"commit": source_commit, "lock_sha256": lock_sha256},
                "target": {"system": platform.system(), "machine": platform.machine()},
                "python": python_block,
                "artifacts": entries,
                "dependency_closure": "complete" if composed else "partial",
                "capabilities": capabilities,
                "hermes_boundary": {
                    "contract": "ADR-0007",
                    "slot_included": False,
                    "activation_owned_by": "managed-hermes-runtime",
                },
                "signing": {"signed": False, "notarized": False},
            }
            if composed:
                manifest["web_payload"] = web_manifest
                manifest["private_access"] = private_access
            if embedded:
                manifest["python_runtime"] = python_runtime
            _write_json(temporary / "manifest.json", manifest)
            components = [
                {
                    "type": "library",
                    "name": entry["name"],
                    "version": entry["version"],
                    "hashes": [{"alg": "SHA-256", "content": entry["sha256"]}],
                }
                for entry in entries
            ]
            if composed:
                adapter_path = web_destination / "server/node-adapter.mjs"
                license_path = web_destination / "licenses/OFL-1.1.txt"
                katex_components, katex_inputs = _katex_supply({
                    relative: web_destination / relative
                    for relative in (KATEX_MANIFEST, KATEX_LICENSE)
                    if (web_destination / relative).is_file()
                })
                components.extend(katex_components)
                components.extend(
                    [
                        {
                            "type": "application",
                            "name": "cortex-web-release",
                            "version": web_build_id,
                            "hashes": [
                                {"alg": "SHA-256", "content": web_manifest["ledger_sha256"]}
                            ],
                            "properties": [
                                {"name": "cortex:payload-root", "value": "artifacts/web"},
                                {
                                    "name": "cortex:package-lock-sha256",
                                    "value": web_lock_sha256,
                                },
                            ],
                        },
                        {
                            "type": "application",
                            "name": "cortex-web-node-adapter",
                            "version": str(node_adapter_version),
                            "hashes": [{"alg": "SHA-256", "content": _sha256(adapter_path)}],
                        },
                        *[
                            {
                                "type": "file",
                                "name": name,
                                "hashes": [{"alg": "SHA-256", "content": digest}],
                                "licenses": [{"license": {"id": "OFL-1.1"}}],
                                "externalReferences": [{"type": "distribution", "url": source}],
                            }
                            for digest, (name, source) in _FONT_COMPONENTS.items()
                        ],
                        {
                            "type": "file",
                            "name": "SIL Open Font License 1.1 terms",
                            "hashes": [{"alg": "SHA-256", "content": _sha256(license_path)}],
                        },
                    ]
                )
            if embedded:
                components.extend(_embedded_sbom_components(python_runtime, temporary))
            sbom = {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": schema_version,
                "metadata": {"component": {"type": "application", "name": "cortex-developer-bundle"}},
                "components": components,
                "properties": [
                    {
                        "name": "cortex:dependency-closure",
                        "value": "complete" if composed else "partial",
                    },
                    {"name": "cortex:unsigned", "value": "true"},
                ],
            }
            _write_json(temporary / "sbom.cdx.json", sbom)
            provenance = {
                "schema_version": schema_version,
                "claim": "inputs-only",
                "source_commit": source_commit,
                "lock_sha256": lock_sha256,
                "build_commands": _build_commands(embedded),
                "signed": False,
                "reproducible_build_proven": False,
            }
            if embedded:
                provenance["python_runtime_inputs"] = _python_runtime_provenance(python_runtime)
                provenance["dependency_inputs"] = {
                    "requirements_sha256": _sha256(temporary / _REQUIREMENTS_RELATIVE),
                    "wheel_count": len(dependency_paths),
                    # Which wheels the index did not supply. Provenance is what
                    # the side record is for, so it belongs where a reader
                    # already looks for "what went into this build".
                    "sdist_built": sorted(sdist_builds or ()),
                }
            if composed:
                provenance["web_build_inputs"] = {
                    "release_build_id": web_build_id,
                    "package_lock_sha256": web_lock_sha256,
                    "payload_ledger_sha256": web_manifest["ledger_sha256"],
                    "node_adapter": {
                        "path": "server/node-adapter.mjs",
                        "version": node_adapter_version,
                        "sha256": _sha256(adapter_path),
                    },
                    **katex_inputs,
                    "font_components": sorted(_FONT_COMPONENTS),
                    "ofl_terms": {
                        "path": "licenses/OFL-1.1.txt",
                        "sha256": _sha256(license_path),
                    },
                }
            _write_json(temporary / "provenance-inputs.json", provenance)
            payloads = sorted(
                path for path in temporary.rglob("*") if path.is_file() and path.name != "checksums.sha256"
            )
            lines = [f"{_sha256(path)}  {path.relative_to(temporary).as_posix()}" for path in payloads]
            (temporary / "checksums.sha256").write_text("\n".join(lines) + "\n")
            verify_bundle(temporary, node_executable=node_executable)
            os.replace(temporary, self.output)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        verified = verify_bundle(self.output, node_executable=node_executable)
        return BundleResult(self.output, verified.digest)


def build_repository_bundle(
    repository: Path,
    output: Path,
    *,
    release_id: str,
    release_sequence: int,
    created_at: str | None = None,
    web_payload_root: Path | None = None,
    web_payload_ledger: Path | None = None,
    web_build_id: str | None = None,
    web_lock_sha256: str | None = None,
    node_adapter_version: int | None = None,
    node_executable: Path | None = None,
    python_runtime_archive: Path | None = None,
    python_runtime_pin: Path | None = None,
    requirements: Path | None = None,
    wheelhouse: Path | None = None,
    sdist_builds: Path | None = None,
) -> BundleResult:
    """Build every workspace wheel offline from an exact clean checkout.

    The production entry point can only emit the embedded tier: the vendored
    interpreter, the exported requirements, and the wheelhouse are required, so
    `cortex-dist build` cannot produce a host-coupled bundle even though
    `BundleBuilder.assemble` retains the legacy tiers for rollback and tests.
    """

    repository = repository.resolve()
    if not (repository / ".git").exists() and not (repository / ".git").is_file():
        raise BundleVerificationError("repository is not a Git checkout")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise BundleVerificationError("repository checkout must be clean")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = revision.stdout.strip()
    if revision.returncode != 0 or not _COMMIT.fullmatch(commit):
        raise BundleVerificationError("repository commit is unavailable")
    lock = repository / "uv.lock"
    if not lock.is_file():
        raise BundleVerificationError("repository lock file is missing")
    if python_runtime_archive is None or python_runtime_pin is None:
        raise BundleVerificationError(
            "vendored Python runtime is missing or unverified; run tools/vendor_python_runtime.py"
        )
    pin = _load_json(Path(python_runtime_pin), "vendored Python runtime pin")
    if requirements is None or wheelhouse is None:
        raise BundleVerificationError(
            "wheelhouse is missing or unverified; run tools/vendor_wheelhouse.py"
        )
    dependency_wheels = tuple(sorted(Path(wheelhouse).glob("*.whl")))
    if not dependency_wheels or not Path(requirements).is_file():
        raise BundleVerificationError(
            "wheelhouse is missing or unverified; run tools/vendor_wheelhouse.py"
        )
    # `tools/vendor_wheelhouse.py` writes its sdist side record into the
    # wheelhouse it just filled, so the default is to read it from there and an
    # explicit path is only needed when the two have been separated. An explicit
    # path that does not exist is a mistake, not an absence, and is refused.
    if sdist_builds is not None:
        record_path: Path | None = Path(sdist_builds)
        if not record_path.is_file():
            raise BundleVerificationError("sdist build record is missing")
    else:
        discovered = Path(wheelhouse) / _SDIST_BUILDS_NAME
        record_path = discovered if discovered.is_file() else None
    sdist_record = (
        _load_json(record_path, "sdist build record") if record_path is not None else None
    )
    # The same rule `_validate_sdist_builds` applies to a bundle's own copy, and
    # for the same reason: a present-but-empty record claims a derivation and
    # then names none. `assemble` writes no bundle file for an empty mapping, so
    # without this the record would be silently discarded here and the bundle
    # would verify as if the wheelhouse had never made the claim.
    if sdist_record is not None and not sdist_record:
        raise BundleVerificationError("sdist build record is empty")
    with tempfile.TemporaryDirectory(prefix="cortex-wheel-build-") as temporary_name:
        wheel_dir = Path(temporary_name)
        for package in _WORKSPACE_DISTRIBUTIONS:
            completed = subprocess.run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--package",
                    package,
                    "--out-dir",
                    str(wheel_dir),
                ],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if completed.returncode != 0:
                raise BundleVerificationError(f"offline wheel build failed for {package}")
        wheels = tuple(wheel_dir.glob("*.whl"))
        return BundleBuilder(output).assemble(
            release_id=release_id,
            release_sequence=release_sequence,
            source_commit=commit,
            lock_sha256=_sha256(lock),
            wheels=wheels,
            created_at=created_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            web_payload_root=web_payload_root,
            web_payload_ledger=web_payload_ledger,
            web_build_id=web_build_id,
            web_lock_sha256=web_lock_sha256,
            node_adapter_version=node_adapter_version,
            node_executable=node_executable,
            python_runtime_archive=Path(python_runtime_archive),
            python_runtime_pin=pin,
            requirements=Path(requirements),
            dependency_wheels=dependency_wheels,
            sdist_builds=sdist_record,
        )


def _load_json(path: Path, label: str) -> dict[str, object]:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise BundleVerificationError(f"{label} is oversized")
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleVerificationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise BundleVerificationError(f"{label} must be an object")
    return value


def _validate_manifest(manifest: dict[str, object], files: dict[str, Path]) -> None:
    schema_version = manifest.get("schema_version")
    expected_fields = (
        _MANIFEST_FIELDS_BY_SCHEMA.get(schema_version) if type(schema_version) is int else None
    )
    if expected_fields is None or set(manifest) != expected_fields:
        raise BundleVerificationError("manifest schema is not closed")
    if manifest.get("channel") != "developer-unsigned":
        raise BundleVerificationError("manifest schema or channel is unsupported")
    if not _IDENTIFIER.fullmatch(str(manifest.get("release_id", ""))):
        raise BundleVerificationError("manifest release identifier is unsafe")
    sequence = manifest.get("release_sequence")
    if type(sequence) is not int or sequence < 1:
        raise BundleVerificationError("manifest release sequence is invalid")
    if manifest.get("dependency_closure") not in {"partial", "complete"}:
        raise BundleVerificationError("manifest dependency closure is invalid")
    if schema_version >= 2 and manifest["dependency_closure"] != "complete":
        raise BundleVerificationError("manifest dependency closure does not match its schema")
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {"commit", "lock_sha256"}:
        raise BundleVerificationError("manifest source schema is invalid")
    if not _COMMIT.fullmatch(str(source["commit"])) or not _SHA256.fullmatch(str(source["lock_sha256"])):
        raise BundleVerificationError("manifest source values are invalid")
    target = manifest.get("target")
    if not isinstance(target, dict) or set(target) != {"system", "machine"}:
        raise BundleVerificationError("manifest target schema is invalid")
    if target != {"system": platform.system(), "machine": platform.machine()}:
        raise BundleVerificationError("bundle target is incompatible with this host")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != {"sqlite_vec", "ocr", "hermes_slot"}:
        raise BundleVerificationError("manifest capability schema is invalid")
    if any(type(value) is not bool for value in capabilities.values()):
        raise BundleVerificationError("manifest capability values are invalid")
    signing = manifest.get("signing")
    if signing != {"signed": False, "notarized": False}:
        raise BundleVerificationError("developer manifest must be explicitly unsigned")
    hermes = manifest.get("hermes_boundary")
    if not isinstance(hermes, dict) or set(hermes) != {
        "contract", "slot_included", "activation_owned_by"
    } or hermes != {
        "contract": "ADR-0007",
        "slot_included": False,
        "activation_owned_by": "managed-hermes-runtime",
    }:
        raise BundleVerificationError("managed Hermes slot boundary is invalid")


def _validate_python_binding(manifest: dict[str, object], files: dict[str, Path]) -> None:
    """Decide which interpreter will run the shipped wheels, and prove it can.

    For schema 1 and 2 this is byte-for-byte the historical check: the wheels
    are ABI-bound and the generation's virtual environment is built from
    whatever interpreter ran the installer, so the verifying host's own
    major.minor is the only thing that can be asserted. That branch is retained
    permanently, so already-published generations keep verifying.

    For schema 3 the host drops out of the statement entirely. The interpreter
    is a named, digest-bound file inside the bundle, and every shipped wheel is
    proven tag-compatible with *it* — a strict superset of what the old check
    concluded, and the reason an install no longer needs a matching host Python.
    """

    python = manifest.get("python")
    if not isinstance(python, dict) or set(python) != {
        "implementation",
        "major",
        "minor",
        "embedded",
    }:
        raise BundleVerificationError("manifest Python schema is invalid")
    embedded = python["embedded"]
    if type(embedded) is not bool:
        raise BundleVerificationError("manifest Python schema is invalid")
    if embedded != (manifest["schema_version"] == 3):
        raise BundleVerificationError("manifest Python schema does not match its bundle schema")
    if not embedded:
        expected_python = {
            "implementation": platform.python_implementation(),
            "major": int(platform.python_version_tuple()[0]),
            "minor": int(platform.python_version_tuple()[1]),
            "embedded": False,
        }
        if python != expected_python:
            raise BundleVerificationError("bundle Python ABI is incompatible with this host")
        return
    runtime = _validate_python_runtime(manifest.get("python_runtime"), files)
    major, minor = _release_pair(str(runtime["version"]))
    if (
        python["implementation"] != "CPython"
        or python["major"] != major
        or python["minor"] != minor
        or runtime["abi_tag"] != f"cp{major}{minor}"
    ):
        raise BundleVerificationError("manifest Python does not match the embedded runtime")
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict) or artifact.get("role") not in _WHEEL_ROLES:
            raise BundleVerificationError("artifact role is unsupported")
        try:
            compatible = wheel_tags_compatible(
                str(artifact["python_tag"]),
                str(artifact["abi_tag"]),
                str(artifact["platform_tag"]),
                major=major,
                minor=minor,
            )
        except WheelClosureError as exc:
            raise BundleVerificationError(str(exc)) from exc
        if not compatible:
            raise BundleVerificationError(
                "artifact ABI or platform tag does not match the embedded interpreter"
            )


def _validate_python_runtime(value: object, files: dict[str, Path]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PYTHON_RUNTIME_FIELDS:
        raise BundleVerificationError("manifest Python runtime schema is invalid")
    upstream = value["upstream"]
    normalization = value["normalization"]
    if (
        not isinstance(upstream, dict)
        or set(upstream) != _PYTHON_RUNTIME_UPSTREAM_FIELDS
        or not isinstance(normalization, dict)
        or set(normalization) != _PYTHON_RUNTIME_NORMALIZATION_FIELDS
    ):
        raise BundleVerificationError("manifest Python runtime schema is invalid")
    if value["policy"] != _PYTHON_RUNTIME_POLICY:
        raise BundleVerificationError("embedded Python runtime policy is invalid")
    relative = value["path"]
    if (
        not isinstance(relative, str)
        or not relative.startswith(_PYTHON_RUNTIME_ROOT)
        or relative not in files
    ):
        raise BundleVerificationError("embedded Python runtime path is invalid")
    carried = sorted(item for item in files if item.startswith(_PYTHON_RUNTIME_ROOT))
    if carried != [relative]:
        raise BundleVerificationError("embedded Python runtime directory is not closed")
    archive = files[relative]
    if value["size"] != archive.stat().st_size or value["sha256"] != _sha256(archive):
        raise BundleVerificationError("embedded Python runtime digest or size mismatch")
    if (
        value["implementation"] != "CPython"
        or value["platform_tag"] != "macosx_11_0_arm64"
        or value["interpreter_path"] != "bin/python3.14"
        or not _SHA256.fullmatch(str(upstream["sha256"]))
        or not _SHA256.fullmatch(str(upstream["attestation_sha256"]))
    ):
        raise BundleVerificationError("embedded Python runtime identity is invalid")
    return value


def _validate_dependency_closure(manifest: dict[str, object], files: dict[str, Path]) -> None:
    """Prove `dependency_closure: "complete"` from the shipped bytes.

    This is a self-consistency property, which is the only notion of closure
    that means anything for an unsigned bundle: forging it requires forging the
    wheels' own METADATA, and a bundle consistent with what it will actually
    install is not a lie about its closure. Two independent derivations must
    agree — the one read out of the wheels, and the one the lock pinned.
    """

    runtime = manifest["python_runtime"]
    version = str(runtime["version"])
    major, minor = _release_pair(version)
    entries: list[ClosureEntry] = []
    dependency_digests: dict[str, str] = {}
    try:
        for artifact in manifest["artifacts"]:
            role = artifact["role"]
            if role not in _WHEEL_ROLES:
                raise BundleVerificationError("artifact role is unsupported")
            metadata = read_wheel_metadata(files[str(artifact["path"])])
            name = normalize_name(metadata.name)
            if name != normalize_name(str(artifact["name"])) or metadata.version != artifact[
                "version"
            ]:
                raise BundleVerificationError("artifact metadata does not match its manifest entry")
            if name in _FORBIDDEN_DISTRIBUTIONS:
                raise BundleVerificationError("forbidden license boundary")
            if metadata.requires_python and not satisfies(version, metadata.requires_python):
                raise BundleVerificationError(
                    "artifact requires a Python the embedded runtime does not provide"
                )
            if not wheel_tags_compatible(
                str(artifact["python_tag"]),
                str(artifact["abi_tag"]),
                str(artifact["platform_tag"]),
                major=major,
                minor=minor,
            ):
                raise BundleVerificationError(
                    "artifact ABI or platform tag does not match the embedded interpreter"
                )
            entries.append(
                ClosureEntry(
                    name=name,
                    version=metadata.version,
                    requires_python=metadata.requires_python,
                    requires_dist=metadata.requires_dist,
                )
            )
            if role == "dependency-wheel":
                dependency_digests[name] = str(artifact["sha256"])
        proof = prove_closure(entries, _WORKSPACE_DISTRIBUTIONS)
        pinned = parse_requirements_hashes(files[_REQUIREMENTS_RELATIVE])
    except WheelClosureError as exc:
        raise BundleVerificationError(f"dependency closure is invalid: {exc}") from exc
    if proof.missing or proof.unsatisfied:
        raise BundleVerificationError("dependency closure is incomplete")
    if proof.unreachable:
        raise BundleVerificationError("dependency closure carries an unreachable distribution")
    if set(pinned) != set(dependency_digests):
        raise BundleVerificationError(
            "the lock-derived and metadata-derived closures disagree"
        )
    # A distribution that publishes no wheel is built from its lock-pinned
    # sdist at acquisition time, so its wheel digest cannot appear in the lock.
    # The side-record says which distribution that was and which sdist it came
    # from. Read it for exactly what it is: PROVENANCE, not proof — an unsigned
    # bundle's side-record is as forgeable as the wheel beside it. What it does
    # buy is that a wheel absent from the lock must be *named* here and bound to
    # an sdist the lock does pin, so an unexplained artifact still fails closed.
    # Every recorded name is necessarily a shipped dependency wheel: the record
    # may only cite names the lock pins, and the equality above already binds
    # the pinned set to the dependency-wheel set.
    built = _validate_sdist_builds(files, pinned)
    for name, digest in dependency_digests.items():
        recorded = built.get(name)
        if recorded is not None and recorded != digest:
            # A record that describes some other wheel excuses nothing. This is
            # the case a stale wheelhouse beside a substituted wheel produces.
            raise BundleVerificationError("sdist build record does not describe the shipped wheel")
        if digest not in pinned[name] and recorded is None:
            raise BundleVerificationError(
                "the lock-derived and metadata-derived closures disagree"
            )


def _validate_sdist_builds(
    files: dict[str, Path],
    pinned: dict[str, set[str]],
) -> dict[str, str]:
    """Read the side record that explains a locally derived dependency wheel.

    The shape is closed and is exactly what `tools/vendor_wheelhouse.py` emits:
    `{"<normalized name>": {"sdist_sha256": ..., "wheel_sha256": ...}}`. Every
    cited sdist digest must be one the exported lock pins for that name, which
    is the whole of what this buys — the wheel was derived here, but its input
    was not. Returns the wheel digest each recorded distribution is bound to.
    """

    if _SDIST_BUILDS_RELATIVE not in files:
        return {}
    record = _load_json(files[_SDIST_BUILDS_RELATIVE], "sdist build record")
    if not record:
        # An empty record is not "no sdist builds" — that state is the file's
        # absence. A present-but-empty file is an unexplained payload.
        raise BundleVerificationError("sdist build record is empty")
    built: dict[str, str] = {}
    for name, entry in record.items():
        if not isinstance(entry, dict) or set(entry) != _SDIST_BUILD_FIELDS:
            raise BundleVerificationError("sdist build record schema is not closed")
        sdist = entry["sdist_sha256"]
        wheel = entry["wheel_sha256"]
        if (
            not isinstance(sdist, str)
            or not isinstance(wheel, str)
            or not _SHA256.fullmatch(sdist)
            or not _SHA256.fullmatch(wheel)
        ):
            raise BundleVerificationError("sdist build record digest is invalid")
        try:
            normalized = normalize_name(name)
        except WheelClosureError as exc:
            # This module only ever promises `BundleVerificationError`, and this
            # call site sits outside the closure proof's own handler, so an
            # unsupported name here would otherwise crash rather than refuse.
            raise BundleVerificationError(f"sdist build record is invalid: {exc}") from exc
        if normalized != name:
            raise BundleVerificationError("sdist build record names are not normalized")
        if sdist not in pinned.get(normalized, set()):
            raise BundleVerificationError("sdist build record cites an unpinned sdist")
        built[normalized] = wheel
    return built


def _validate_capabilities(manifest: dict[str, object], files: dict[str, Path]) -> None:
    """Re-derive the advertised capabilities from the shipped artifact names.

    Older tiers only type-check this field, which is why it could advertise
    `sqlite_vec: false` while shipping the wheel — or the reverse. The
    derivation is driven by `manifest["target"]`, never the host, so the answer
    is the same wherever the bundle is verified.
    """

    target = manifest["target"]
    if not isinstance(target, dict):
        raise BundleVerificationError("manifest capabilities do not match the artifacts")
    wheels = [
        files[str(artifact["path"])]
        for artifact in manifest["artifacts"]
        if isinstance(artifact, dict) and str(artifact["path"]) in files
    ]
    derived = artifact_capabilities(
        wheels,
        system=str(target["system"]),
        machine=str(target["machine"]),
    )
    if manifest["capabilities"] != derived:
        raise BundleVerificationError("manifest capabilities do not match the artifacts")


def _validate_private_access(
    value: object,
    files: dict[str, Path],
    artifacts: list[object],
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "wheel_path", "wheel_sha256", "required_modules", "console_scripts"
    }:
        raise BundleVerificationError("private-access schema is not closed")
    if value["required_modules"] != _PRIVATE_ACCESS_MODULES or value["console_scripts"] != _PRIVATE_ACCESS_SCRIPTS:
        raise BundleVerificationError("private-access contract is invalid")
    wheel_path = value["wheel_path"]
    wheel_sha256 = value["wheel_sha256"]
    matching = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("role") == "cortex-wheel"
        and artifact.get("path") == wheel_path
        and artifact.get("sha256") == wheel_sha256
    ]
    if len(matching) != 1 or not isinstance(wheel_path, str) or wheel_path not in files:
        raise BundleVerificationError("private-access wheel binding is invalid")
    artifact = matching[0]
    wheel_name, wheel_version = _wheel_details(files[wheel_path])[:2]
    if (
        wheel_name != "cortex"
        or artifact.get("name") != wheel_name
        or artifact.get("version") != wheel_version
    ):
        raise BundleVerificationError("private-access wheel binding is invalid")
    entry_point_path = (
        f"{wheel_name.replace('-', '_')}-{wheel_version}.dist-info/entry_points.txt"
    )
    try:
        with zipfile.ZipFile(files[wheel_path]) as archive:
            member_details = archive.infolist()
            members = [member.filename for member in member_details]
            normalized_members = [
                unicodedata.normalize(
                    "NFC",
                    unicodedata.normalize("NFC", member.removesuffix("/")).casefold(),
                )
                for member in members
            ]
            if len(members) != len(set(members)) or len(normalized_members) != len(
                set(normalized_members)
            ):
                raise BundleVerificationError("private-access wheel has duplicate members")
            for member in member_details:
                relative = member.filename.removesuffix("/")
                parts = relative.split("/")
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                expected_type = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
                if (
                    not relative
                    or member.flag_bits & 0x1
                    or member.filename.startswith("/")
                    or "\\" in member.filename
                    or "\0" in member.filename
                    or any(part in {"", ".", ".."} for part in parts)
                    or re.fullmatch(r"[A-Za-z]:.*", parts[0]) is not None
                    or file_type not in {0, expected_type}
                ):
                    raise BundleVerificationError("private-access wheel member is unsafe")
            if not set(_PRIVATE_ACCESS_MODULES).issubset(members):
                raise BundleVerificationError("private-access wheel is missing required modules")
            if any(archive.getinfo(module).is_dir() for module in _PRIVATE_ACCESS_MODULES):
                raise BundleVerificationError("private-access wheel has invalid required modules")
            entry_points = [
                member for member in members if member.endswith(".dist-info/entry_points.txt")
            ]
            if entry_points != [entry_point_path]:
                raise BundleVerificationError("private-access wheel entry points are invalid")
            if archive.getinfo(entry_point_path).file_size > 1024 * 1024:
                raise BundleVerificationError("private-access wheel entry points are invalid")
            text = archive.read(entry_point_path).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise BundleVerificationError("private-access wheel metadata is unreadable") from exc
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
        if parser.defaults() or not parser.has_section("console_scripts"):
            raise BundleVerificationError("private-access wheel console scripts are missing")
        observed = dict(parser.items("console_scripts", raw=True))
    except configparser.Error as exc:
        raise BundleVerificationError("private-access wheel metadata is unreadable") from exc
    if any(observed.get(name) != target for name, target in _PRIVATE_ACCESS_SCRIPTS.items()):
        raise BundleVerificationError("private-access wheel console scripts are invalid")


def _validate_web_payload(
    value: object,
    files: dict[str, Path],
    root: Path,
    node_executable: Path | None,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "root",
        "ledger_path",
        "ledger_sha256",
        "release_build_id",
        "package_lock_sha256",
        "node_adapter_version",
        "node_policy",
    }:
        raise BundleVerificationError("Web payload schema is not closed")
    if (
        value["root"] != "artifacts/web"
        or value["ledger_path"] != "artifacts/web-payload.sha256"
        or value["node_policy"] != _NODE_POLICY
        or not _WEB_BUILD_ID.fullmatch(str(value["release_build_id"]))
        or not _SHA256.fullmatch(str(value["package_lock_sha256"]))
        or type(value["node_adapter_version"]) is not int
        or value["node_adapter_version"] != _NODE_ADAPTER_VERSION
    ):
        raise BundleVerificationError("Web payload metadata is invalid")
    ledger_path = str(value["ledger_path"])
    if ledger_path not in files or value["ledger_sha256"] != _sha256(files[ledger_path]):
        raise BundleVerificationError("Web payload ledger binding is invalid")
    entries = _parse_web_ledger(files[ledger_path])
    prefix = "artifacts/web/"
    observed = {relative.removeprefix(prefix) for relative in files if relative.startswith(prefix)}
    if set(entries) != observed:
        raise BundleVerificationError("Web payload ledger coverage is invalid")
    if not _REQUIRED_WEB_PATHS.issubset(entries):
        raise BundleVerificationError("required Web payload path is missing")
    for relative, (digest, size) in entries.items():
        payload = files[prefix + relative]
        if payload.stat().st_size != size or _sha256(payload) != digest:
            raise BundleVerificationError("Web payload checksum or size mismatch")
    _validate_web_semantics(
        entries,
        files,
        prefix,
        str(value["release_build_id"]),
        int(value["node_adapter_version"]),
        root,
        node_executable,
    )


def _recorded_build_commands(
    provenance: dict[str, object], artifacts: list[object], embedded: bool
) -> list[str]:
    """Read an ALREADY-INSTALLED generation's own record of what it built.

    Every other field of `expected_provenance` is derived from the bundle under
    verification — its manifest, its ledger, its wheels, its Web payload — so a
    forged record is held to bytes this verifier computes for itself.
    `build_commands` is the one field derived from the VERIFIER's
    `_WORKSPACE_DISTRIBUTIONS` instead, which makes it a statement about the
    workspace THIS checkout builds rather than about the bundle in front of it.
    For a bundle being admitted that is exactly right. For a predecessor it is
    the same trap `_verify_bundled_tools` documents one layer down: a generation
    composed from a different workspace could never be upgraded over, because
    the new verifier would measure the old bundle against its own member list.
    The research-only extraction is precisely such a change, so a candidate
    built from it refuses every generation built from the wider composition.

    So for an installed predecessor the recorded list is read rather than
    re-derived — and it is still not taken on faith. It must be internally
    consistent with the bundle carrying it: every command has the exact shape
    the builder emits, an embedded bundle's dependency export is byte-identical,
    and the package names are exactly the workspace wheels the manifest declares
    (`_WORKSPACE_WHEEL_ROLES`), each of which `verify_bundle` has already
    size- and digest-matched against the file on disk. A record naming a package
    the bundle does not carry, dropping one it does, or smuggling any other
    command is refused.

    What that concedes is the claim "these are the members this checkout builds",
    which for a predecessor was never true and was never what the field was for.
    What it keeps is the claim that matters: this bundle was built from these
    packages, and these packages are the ones it ships. The predecessor's
    identity is anchored independently regardless — by the distribution
    pointer's `bundle_digest` and by the checksum ledger this verifier
    re-derives over every file, `provenance-inputs.json` included — so the
    record cannot be edited after installation without the digest ceasing to
    match. Only schema 2 and above reach here, and there the ledger IS the
    identity, so that anchor always holds.
    """

    recorded = provenance.get("build_commands")
    if not isinstance(recorded, list):
        raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
    commands = list(recorded)
    if embedded:
        if not commands or commands[-1] != _EXPORT_COMMAND:
            raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
        commands = commands[:-1]
    built: list[str] = []
    for command in commands:
        if not isinstance(command, str) or not command.startswith(_BUILD_COMMAND_PREFIX):
            raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
        built.append(normalize_name(command.removeprefix(_BUILD_COMMAND_PREFIX)))
    # Sorted lists, not sets: a repeated name must not pass by collapsing into a
    # shipped one, and the recorded order carries no claim this can check.
    shipped = sorted(
        normalize_name(str(artifact["name"]))
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("role") in _WORKSPACE_WHEEL_ROLES
    )
    if sorted(built) != shipped:
        raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
    return list(recorded)


def _validate_composed_supply_chain(
    sbom: dict[str, object],
    provenance: dict[str, object],
    manifest: dict[str, object],
    artifacts: list[object],
    files: dict[str, Path],
    *,
    installed: bool = False,
) -> None:
    schema_version = manifest["schema_version"]
    embedded = schema_version == 3
    web = manifest["web_payload"]
    if not isinstance(web, dict):
        raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
    wheel_components = [
        {
            "type": "library",
            "name": artifact["name"],
            "version": artifact["version"],
            "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
        }
        for artifact in artifacts
        if isinstance(artifact, dict)
    ]
    adapter_digest = _sha256(files["artifacts/web/server/node-adapter.mjs"])
    license_digest = _sha256(files["artifacts/web/licenses/OFL-1.1.txt"])
    katex_components, katex_inputs = _katex_supply(files, "artifacts/web/")
    composed_components = [
        *katex_components,
        {
            "type": "application",
            "name": "cortex-web-release",
            "version": web["release_build_id"],
            "hashes": [{"alg": "SHA-256", "content": web["ledger_sha256"]}],
            "properties": [
                {"name": "cortex:payload-root", "value": "artifacts/web"},
                {"name": "cortex:package-lock-sha256", "value": web["package_lock_sha256"]},
            ],
        },
        {
            "type": "application",
            "name": "cortex-web-node-adapter",
            "version": str(web["node_adapter_version"]),
            "hashes": [{"alg": "SHA-256", "content": adapter_digest}],
        },
        *[
            {
                "type": "file",
                "name": name,
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "licenses": [{"license": {"id": "OFL-1.1"}}],
                "externalReferences": [{"type": "distribution", "url": source}],
            }
            for digest, (name, source) in _FONT_COMPONENTS.items()
        ],
        {
            "type": "file",
            "name": "SIL Open Font License 1.1 terms",
            "hashes": [{"alg": "SHA-256", "content": license_digest}],
        },
    ]
    if embedded:
        composed_components.extend(
            _embedded_sbom_components(manifest["python_runtime"], files["manifest.json"].parent)
        )
    expected_sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": schema_version,
        "metadata": {"component": {"type": "application", "name": "cortex-developer-bundle"}},
        "components": wheel_components + composed_components,
        "properties": [
            {"name": "cortex:dependency-closure", "value": "complete"},
            {"name": "cortex:unsigned", "value": "true"},
        ],
    }
    source = manifest["source"]
    if not isinstance(source, dict):
        raise BundleVerificationError("schema-2 supply-chain metadata is invalid")
    build_commands = (
        _recorded_build_commands(provenance, artifacts, embedded)
        if installed
        else _build_commands(embedded)
    )
    expected_provenance = {
        "schema_version": schema_version,
        "claim": "inputs-only",
        "source_commit": source["commit"],
        "lock_sha256": source["lock_sha256"],
        "build_commands": build_commands,
        "signed": False,
        "reproducible_build_proven": False,
        "web_build_inputs": {
            "release_build_id": web["release_build_id"],
            "package_lock_sha256": web["package_lock_sha256"],
            "payload_ledger_sha256": web["ledger_sha256"],
            "node_adapter": {
                "path": "server/node-adapter.mjs",
                "version": web["node_adapter_version"],
                "sha256": adapter_digest,
            },
            **katex_inputs,
            "font_components": sorted(_FONT_COMPONENTS),
            "ofl_terms": {
                "path": "licenses/OFL-1.1.txt",
                "sha256": license_digest,
            },
        },
    }
    if embedded:
        expected_provenance["python_runtime_inputs"] = _python_runtime_provenance(
            manifest["python_runtime"]
        )
        expected_provenance["dependency_inputs"] = {
            "requirements_sha256": _sha256(files[_REQUIREMENTS_RELATIVE]),
            "wheel_count": sum(
                1
                for artifact in artifacts
                if isinstance(artifact, dict) and artifact.get("role") == "dependency-wheel"
            ),
            # `_validate_dependency_closure` has already proven this record's
            # shape by the time supply-chain metadata is re-derived, so reading
            # its names back is a derivation and not a second trust decision.
            "sdist_built": sorted(
                _load_json(files[_SDIST_BUILDS_RELATIVE], "sdist build record")
            )
            if _SDIST_BUILDS_RELATIVE in files
            else [],
        }
    if sbom != expected_sbom or provenance != expected_provenance:
        raise BundleVerificationError("schema-2 supply-chain metadata is invalid")


def _verify_bundled_tools(files: dict[str, Path]) -> None:
    """Pin the tools the bundle carries to this verifier's own copies.

    A bundle ships `tools/distribution/`, and an installed generation verifies
    itself with THAT copy — so for the in-bundle verifier the pinned parser digest
    is self-attestation, not a defence. This verifier, running from a checkout,
    therefore refuses a bundle whose tools differ from its own by a single byte.
    That closes the substituted-parser forgery at the point a bundle is admitted.

    A bundle verifying itself cannot benefit from this check. Verify a received
    bundle with an independent checkout, never with the `cortex-dist` it carries.

    An ALREADY-INSTALLED generation is exempt (`verify_bundle(pin_tools=False)`)
    because it is not being admitted: its identity is anchored by the pointer's
    `bundle_digest` and by the ledger this verifier re-derives over every file,
    `tools/**` included. Pinning it instead makes a cross-generation upgrade
    impossible whenever `distribution/` changes, because the new generation's
    verifier would be measuring the previous generation against its own tools.
    """

    source = Path(__file__).resolve().parent
    expected: dict[str, bytes] = {}
    try:
        for path in sorted(source.glob("*.py")):
            expected[f"tools/distribution/{path.name}"] = path.read_bytes()
        for relative in (_WEB_ANALYSER_RELATIVE, _ACORN_RELATIVE, "vendor/acorn-LICENSE.txt"):
            expected[f"tools/distribution/{relative}"] = (source / relative).read_bytes()
        # `source.parent` is the repository root in a checkout and the bundle's
        # own `tools/` in a bundle, so the staging kernel is pinned by the same
        # expression from either side.
        expected[f"tools/{_TOOLS_PLATFORM_PACKAGE}/__init__.py"] = (
            _TOOLS_PLATFORM_MARKER.encode()
        )
        expected[f"tools/{_TOOLS_RUNTIME_STAGING_RELATIVE}"] = (
            source.parent / _TOOLS_RUNTIME_STAGING_RELATIVE
        ).read_bytes()
    except OSError as exc:
        raise BundleVerificationError("verifier distribution tools are unreadable") from exc
    # The entrypoints decide what code `./cortex-dist` actually runs, so they are
    # pinned too. Both are generated deterministically, so regenerating them here
    # is an exact expectation rather than a second copy to keep in step.
    expected["tools/cortex_dist.py"] = _TOOLS_ENTRYPOINT.encode()
    expected["cortex-dist"] = _TOOLS_LAUNCHER.encode()
    shipped = {
        relative
        for relative in files
        if relative.startswith("tools/") or relative == "cortex-dist"
    }
    if shipped != set(expected):
        raise BundleVerificationError("bundled distribution tools do not match the verifier")
    for relative, contents in expected.items():
        try:
            if files[relative].read_bytes() != contents:
                raise BundleVerificationError("bundled distribution tools do not match the verifier")
        except OSError as exc:
            raise BundleVerificationError("bundled distribution tools are unreadable") from exc


def _verify_wheel_staging_kernel(files: dict[str, Path]) -> None:
    """Pin the wheel's copy of the staging kernel to the bundle's `tools/` copy.

    ⟦AMD-2b⟧'s residual, owed once the wheel copy gained an executor. The kernel
    travels twice — `tools/cortex_platform/runtime_staging.py` for the installer,
    which runs under a host interpreter with only `tools/` on `sys.path`, and
    inside the cortex wheel for the installed product's `cortex runtime stage`.
    Until this slice only the first copy was verified; the second's byte-equality
    was a *constructive* property of `build_repository_bundle` (clean checkout
    enforced, every wheel built by `uv build` from the same tree in the same
    call) rather than a checked one.

    Now that `cortex runtime stage` expands a slot's interpreter through the
    wheel's copy, a bundle could ship an installer that seals and probes
    correctly beside a product that does not, and nothing would say so. Chained
    with `_verify_bundled_tools`, which pins the `tools/` copy to the verifying
    checkout, this makes all three copies one.
    """

    wheels = sorted(
        relative
        for relative in files
        if relative.startswith("artifacts/wheels/") and relative.endswith(".whl")
        and _wheel_details(files[relative])[0] == "cortex"
    )
    if len(wheels) != 1:
        raise BundleVerificationError("bundle does not carry exactly one cortex wheel")
    expected = files.get(f"tools/{_TOOLS_RUNTIME_STAGING_RELATIVE}")
    if expected is None:
        # Reachable, and the reason this call sits inside `if pin_tools:`. The
        # old `# pragma: no cover - _verify_bundled_tools ran first` encoded an
        # assumption that only holds while the pin runs: with `pin_tools=False`
        # the tools are whatever the installed generation shipped, and a
        # generation older than the staging kernel shipped none.
        raise BundleVerificationError("bundle carries no staging kernel in its tools")
    try:
        with zipfile.ZipFile(files[wheels[0]]) as archive:
            shipped = archive.read(_TOOLS_RUNTIME_STAGING_RELATIVE)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise BundleVerificationError(
            "cortex wheel does not carry the staging kernel"
        ) from exc
    try:
        if shipped != expected.read_bytes():
            raise BundleVerificationError(
                "cortex wheel staging kernel differs from the bundled tools"
            )
    except OSError as exc:
        raise BundleVerificationError("bundled staging kernel is unreadable") from exc


def verify_bundle(
    path: Path,
    *,
    node_executable: Path | None = None,
    pin_tools: bool = True,
) -> VerifiedBundle:
    """Verify closed payload coverage before any install-time execution.

    `pin_tools=False` exempts a generation already installed under a pointer
    that records its digest from every check that measures the bundle against
    THIS CHECKOUT's own source rather than against the bundle's own bytes —
    today `_verify_bundled_tools` and `_verify_wheel_staging_kernel`, which
    measure its `tools/`, and the `build_commands` derivation in
    `_validate_composed_supply_chain`, which measures its workspace
    composition. That is the rule, not the list: a later check of the same kind
    belongs inside the guard, and a check derived from the bundle itself stays
    outside it and is unchanged. See `_verify_bundled_tools` and
    `_recorded_build_commands` for why an installed generation is already
    anchored without them, and what each exemption still holds it to.
    """

    if path.is_symlink():
        raise BundleVerificationError("bundle root is a symlink")
    path = path.resolve()
    if not path.is_dir():
        raise BundleVerificationError("bundle is not a directory")
    files: dict[str, Path] = {}
    for item in path.rglob("*"):
        details = item.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise BundleVerificationError("bundle contains a symlink")
        if item.is_dir():
            continue
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise BundleVerificationError("bundle contains a special or linked file")
        relative = item.relative_to(path).as_posix()
        files[relative] = item
    required = {"manifest.json", "checksums.sha256", "sbom.cdx.json", "provenance-inputs.json", "cortex-dist"}
    if not required.issubset(files):
        raise BundleVerificationError("bundle is missing required payloads")
    if pin_tools:
        _verify_bundled_tools(files)
        # ⟦S32-01⟧ Inside the guard, not beside it. The natural resolution puts
        # this call one line below and outside, and that was measured against
        # the real gen-7 bundle: it carries no `tools/cortex_platform/` at all,
        # so `_verify_installed_version` — which is the whole reason `pin_tools`
        # exists — would refuse an already-installed generation with "bundle
        # carries no staging kernel in its tools", and gen7→gen8 doctor and
        # upgrade would stop. The check belongs to admission, like the pin it
        # follows: an installed generation is anchored by its pointer digest and
        # by the ledger, both of which already cover these bytes.
        _verify_wheel_staging_kernel(files)
    try:
        checksum_lines = files["checksums.sha256"].read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise BundleVerificationError("checksum ledger is unreadable") from exc
    checksums: dict[str, str] = {}
    for line in checksum_lines:
        digest, separator, relative = line.partition("  ")
        if not separator or not _SHA256.fullmatch(digest) or relative in checksums:
            raise BundleVerificationError("checksum ledger is malformed")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise BundleVerificationError("checksum path is unsafe")
        checksums[relative] = digest
    observed = set(files) - {"checksums.sha256"}
    if set(checksums) != observed:
        raise BundleVerificationError("bundle contains undeclared or uncovered payloads")
    for relative, expected in checksums.items():
        if _sha256(files[relative]) != expected:
            raise BundleVerificationError("bundle checksum mismatch")
    manifest = _load_json(files["manifest.json"], "manifest")
    _validate_manifest(manifest, files)
    schema_version = manifest["schema_version"]
    if schema_version == 1 and (
        "artifacts/web-payload.sha256" in files
        or any(relative.startswith("artifacts/web/") for relative in files)
    ):
        raise BundleVerificationError("schema-1 bundle must be wheel-only")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BundleVerificationError("manifest artifacts are invalid")
    # Structural, not value-based: a downgraded manifest cannot shed the files
    # that only the newer tier ships, so the presence of those files is what
    # decides, exactly as the schema-1 guard above already does.
    if schema_version in {1, 2} and (
        _REQUIREMENTS_RELATIVE in files
        or _SDIST_BUILDS_RELATIVE in files
        or any(relative.startswith(_PYTHON_RUNTIME_ROOT) for relative in files)
        or any(
            isinstance(artifact, dict)
            and ("abi_tag" in artifact or artifact.get("role") == "dependency-wheel")
            for artifact in artifacts
        )
    ):
        raise BundleVerificationError("legacy bundle must not carry an embedded Python runtime")
    if schema_version == 3 and not (
        _REQUIREMENTS_RELATIVE in files
        and "artifacts/web-payload.sha256" in files
        and any(relative.startswith(_PYTHON_RUNTIME_ROOT) for relative in files)
    ):
        raise BundleVerificationError("schema-3 bundle is incomplete")
    artifact_fields = _ARTIFACT_FIELDS_V3 if schema_version == 3 else _ARTIFACT_FIELDS
    artifact_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != artifact_fields:
            raise BundleVerificationError("artifact schema is not closed")
        relative = artifact["path"]
        if not isinstance(relative, str) or relative in artifact_paths or relative not in files:
            raise BundleVerificationError("artifact path is invalid")
        artifact_paths.add(relative)
        if artifact["size"] != files[relative].stat().st_size or artifact["sha256"] != _sha256(files[relative]):
            raise BundleVerificationError("artifact checksum or size mismatch")
    # Placed here for two independent reasons, both load-bearing.
    #
    # After the artifact loop: this check indexes an artifact's tag fields
    # behind nothing but a `role` guard, so running it before the closed-schema
    # loop above let a manifest missing `python_tag`/`abi_tag`/`platform_tag`
    # leave `verify_bundle` as a bare `KeyError` instead of a refusal — the
    # failure class A15 exists to eliminate.
    #
    # After the structural guards: the schema-1/2 branch compares the manifest
    # against the VERIFYING HOST's Python, so running it first made a forged
    # schema-3-to-schema-2 downgrade report "bundle Python ABI is incompatible
    # with this host" on any host whose major.minor differed from the bundle's —
    # blaming the operator's interpreter for a forged manifest, and making the
    # refusal reason host-dependent inside the one feature whose whole point is
    # that the host has dropped out of the statement.
    _validate_python_binding(manifest, files)
    if schema_version >= 2:
        _validate_web_payload(
            manifest["web_payload"],
            files,
            path / "artifacts" / "web",
            node_executable,
        )
        _validate_private_access(manifest["private_access"], files, artifacts)
    if schema_version == 3:
        _validate_dependency_closure(manifest, files)
        _validate_capabilities(manifest, files)
    sbom = _load_json(files["sbom.cdx.json"], "SBOM")
    provenance = _load_json(files["provenance-inputs.json"], "provenance")
    if sbom.get("bomFormat") != "CycloneDX" or provenance.get("claim") != "inputs-only":
        raise BundleVerificationError("SBOM or provenance claim is invalid")
    if schema_version >= 2:
        # `installed=not pin_tools`: the same fact, that this bundle is a
        # generation already anchored by a pointer rather than one being
        # admitted, decides both exemptions. See `_recorded_build_commands`.
        _validate_composed_supply_chain(
            sbom, provenance, manifest, artifacts, files, installed=not pin_tools
        )
    identity = files["checksums.sha256"] if schema_version >= 2 else files["manifest.json"]
    # The installed-generation exemption holds only where the identity IS the
    # ledger. A schema-1 bundle is identified by its manifest, which no
    # `tools/` byte enters, so a rewritten ledger there would leave the pointer
    # digest untouched; a legacy generation therefore keeps the pin.
    if not pin_tools and schema_version < 2:
        _verify_bundled_tools(files)
    return VerifiedBundle(path, hashlib.sha256(identity.read_bytes()).hexdigest(), manifest, sbom, provenance)
