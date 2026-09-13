"""Soundness regressions for the Web closure analyser.

Every payload in `PROVEN_ESCAPES` defeated the hand-written JavaScript tokenizer
this analyser replaced: the tokenizer mis-decided the ECMAScript
regex-versus-division ambiguity, so a real, Node-executed `import(...)` — or a
rogue release marker, or a second adapter declaration — could hide inside a
token the tokenizer believed was inert. A real parser is the only sound
discriminator, because the shipped payload legitimately carries import/export
prose inside string, template, and regular-expression literals.

See docs/plans/2026-07-29-web-closure-acorn-gate.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from distribution import bundle as bundle_module
from distribution.bundle import BundleBuilder, BundleVerificationError, verify_bundle

WEB_LOCK_SHA256 = "3" * 64

BT = "`"

# Each case appends JavaScript to a payload file. The analyser must reject all
# of them. The first four hide a live module reference; the last two hide
# release metadata. All six were verified to escape the previous tokenizer.
PROVEN_ESCAPES = [
    (
        "template-swallow-dynamic-import",
        "server/index.js",
        f'if(x)/{BT}/;import("./evilmod.mjs");//{BT}/.test(z)\n',
    ),
    (
        "template-swallow-static-import",
        "server/index.js",
        f'if(x)/{BT}/;import def from "evilpkg";//{BT}/.test(z)\n',
    ),
    (
        "template-swallow-export-from",
        "server/index.js",
        f'if(x)/{BT}/;export {{a}} from "evilpkg";//{BT}/.test(z)\n',
    ),
    (
        "regex-swallow-postfix-increment",
        "server/index.js",
        'let a = 1; a++ / import("child_process") / 1;\n',
    ),
    (
        "hidden-rogue-release-marker",
        "server/index.js",
        f'if(x)/{BT}/;const spoof = "cortex-r0-build-EVIL";//{BT}/.test(z)\n',
    ),
    (
        "hidden-nested-adapter-declaration",
        "server/node-adapter.mjs",
        "if (false) { const ADAPTER_VERSION = 99; }\n",
    ),
]

# Shapes the shipped Vinext payload really contains. None is a module escape and
# all must be accepted, so no text-pattern backstop may be reintroduced.
LEGITIMATE_SHAPES = [
    (
        "regex-literal-matching-import",
        "client/assets/app.js",
        'export const re = /import\\("([^"]+)"\\)/;\n',
    ),
    (
        "import-prose-in-template",
        "client/assets/app.js",
        f"export const m = {BT}Unexpectedly client reference export 'X' is called{BT};\n",
    ),
    (
        "import-export-as-member-and-method",
        "client/assets/app.js",
        "const registry = { import: 1, export: 2 };\n"
        "export class Threads { import(entry) { return entry; } export() { return this; } }\n"
        "export const read = [registry.import, registry.export];\n",
    ),
    (
        "no-substitution-template-dynamic-import",
        "server/index.js",
        f"export const lazy = () => import({BT}./ssr/index.js{BT});\n",
    ),
]


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


def _compose(
    output: Path,
    wheels: tuple[Path, Path],
    web_root: Path,
    web_ledger: Path,
    node: Path | None,
) -> BundleBuilder:
    return BundleBuilder(output).assemble(
        release_id="cortex-dev-2",
        release_sequence=2,
        source_commit="1" * 40,
        lock_sha256="2" * 64,
        wheels=wheels,
        created_at="2026-07-23T12:00:00Z",
        web_payload_root=web_root,
        web_payload_ledger=web_ledger,
        web_build_id="cortex-r0-build-1",
        web_lock_sha256=WEB_LOCK_SHA256,
        node_adapter_version=1,
        node_executable=node,
    )


@pytest.mark.parametrize(("case", "relative", "attack"), PROVEN_ESCAPES)
def test_analyser_rejects_payloads_that_escaped_the_tokenizer(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    attack: str,
) -> None:
    web_root, web_ledger = web_closure
    target = web_root / relative
    target.write_text(target.read_text() + attack)
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        _compose(tmp_path / f"bundle-{case}", wheel_pair, web_root, web_ledger, analyser_node)


@pytest.mark.parametrize(("case", "relative", "addition"), LEGITIMATE_SHAPES)
def test_analyser_accepts_real_vinext_shapes(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
    relative: str,
    addition: str,
) -> None:
    web_root, web_ledger = web_closure
    target = web_root / relative
    target.write_text(target.read_text() + addition)
    _rewrite_web_ledger(web_root, web_ledger)

    result = _compose(tmp_path / f"bundle-{case}", wheel_pair, web_root, web_ledger, analyser_node)

    assert verify_bundle(result.path, node_executable=analyser_node).manifest["schema_version"] == 2


def test_composed_bundle_verification_fails_closed_without_a_node_executable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    result = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)

    with pytest.raises(BundleVerificationError, match="requires a Node executable"):
        verify_bundle(result.path)


def test_composition_fails_closed_without_a_node_executable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
) -> None:
    web_root, web_ledger = web_closure

    with pytest.raises(BundleVerificationError, match="requires a Node executable"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, None)


@pytest.mark.parametrize("case", ["relative", "missing", "not-executable"])
def test_analyser_rejects_an_unusable_node_executable(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    case: str,
) -> None:
    web_root, web_ledger = web_closure
    result = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)
    if case == "relative":
        candidate = Path("node")
        expected = "must be an absolute path"
    elif case == "missing":
        candidate = tmp_path / "absent-node"
        expected = "is unusable"
    else:
        candidate = tmp_path / "inert-node"
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o644)
        expected = "is unusable"

    with pytest.raises(BundleVerificationError, match=expected):
        verify_bundle(result.path, node_executable=candidate)


def test_analyser_rejects_a_tampered_parser(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parser digest is pinned in source so a substituted copy cannot be used
    # to report a clean closure for a dirty payload.
    web_root, web_ledger = web_closure
    result = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)
    monkeypatch.setattr(bundle_module, "_ACORN_SHA256", "0" * 64)

    with pytest.raises(BundleVerificationError, match="parser digest mismatch"):
        verify_bundle(result.path, node_executable=analyser_node)


def test_analyser_rejects_unparseable_payload_javascript(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    target = web_root / "server/index.js"
    target.write_text(target.read_text() + "export const broken = (;\n")
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure JavaScript syntax"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)


def test_analysis_cache_is_content_keyed_and_cannot_mask_tampering(
    tmp_path: Path,
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # The analysis is memoised so a lifecycle operation that revalidates its
    # generation pays for the parse once. The key is the payload bytes, so a
    # tampered payload must never be answered from the cache.
    web_root, _ledger = web_closure
    relatives = sorted(
        path.relative_to(web_root).as_posix()
        for path in web_root.rglob("*")
        if path.is_file() and path.suffix in {".js", ".mjs"}
    )

    first = bundle_module._analyse_web_javascript(analyser_node, web_root, relatives)
    assert bundle_module._analyse_web_javascript(analyser_node, web_root, relatives) is first

    target = web_root / "server/index.js"
    target.write_text(target.read_text() + f'if(x)/{BT}/;import("./evil.js");//{BT}/.test(z)\n')
    reanalysed = bundle_module._analyse_web_javascript(analyser_node, web_root, relatives)

    assert reanalysed is not first
    assert "./evil.js" in [
        reference["specifier"]
        for facts in reanalysed.values()
        for reference in facts["references"]
    ]


def test_vendored_parser_matches_its_pinned_digest() -> None:
    parser = Path(bundle_module.__file__).resolve().parent / bundle_module._ACORN_RELATIVE
    assert hashlib.sha256(parser.read_bytes()).hexdigest() == bundle_module._ACORN_SHA256


#: A real Rolldown/Vinext release payload, staged by whoever runs this gate.
#: Named by the environment rather than by an absolute path on one machine:
#: the path this used to carry made the gate silently permanent-skip anywhere
#: else, which is the same defect `tests/conftest.py` fixed for the worker
#: runtime. Absence stays a skip; a variable pointing at nothing is a skip that
#: names what is missing.
_RETAINED_PAYLOAD_VARIABLE = "CORTEX_TEST_VINEXT_PAYLOAD"
_retained_payload_value = os.environ.get(_RETAINED_PAYLOAD_VARIABLE, "")
RETAINED_PAYLOAD = Path(_retained_payload_value) if _retained_payload_value else None


@pytest.mark.skipif(
    RETAINED_PAYLOAD is None or not RETAINED_PAYLOAD.is_dir(),
    reason=f"a real Vinext payload directory in ${_RETAINED_PAYLOAD_VARIABLE} is required",
)
def test_real_payload_module_graph_is_closed(analyser_node: Path) -> None:
    """Gate the analyser against the real Rolldown/Vinext release payload."""

    relatives = sorted(
        path.relative_to(RETAINED_PAYLOAD).as_posix()
        for path in RETAINED_PAYLOAD.rglob("*")
        if path.is_file() and path.suffix in {".js", ".mjs"}
    )
    ledger = {
        path.relative_to(RETAINED_PAYLOAD).as_posix()
        for path in RETAINED_PAYLOAD.rglob("*")
        if path.is_file()
    }

    analysis = bundle_module._analyse_web_javascript(analyser_node, RETAINED_PAYLOAD, relatives)

    assert len(analysis) == 20
    assert not [facts for facts in analysis.values() if facts["nonLiteralReferences"]]
    assert sum(facts["metaProperties"] for facts in analysis.values()) == 6
    strings = [
        reference
        for facts in analysis.values()
        for reference in facts["references"]
        if reference["literal"] == "string"
    ]
    templates = [
        reference
        for facts in analysis.values()
        for reference in facts["references"]
        if reference["literal"] == "template"
    ]
    assert (len(strings), len(templates)) == (44, 12)
    for relative, facts in analysis.items():
        for reference in facts["references"]:
            bundle_module._validate_module_specifier(relative, reference["specifier"], ledger)
    markers = {
        value
        for value in analysis["server/index.js"]["stringLiterals"]
        if bundle_module._RELEASE_BUILD_MARKER.fullmatch(value)
    }
    assert markers == {"cortex-r0-build-cb058f0"}
    assert analysis["server/node-adapter.mjs"]["adapterDeclarations"] == [
        {"kind": "declarator", "declaration": "const", "value": 1, "topLevel": True}
    ]


def test_analyser_helper_never_reads_the_filesystem(
    tmp_path: Path,
    analyser_node: Path,
) -> None:
    # The caller reads each file once and sends its bytes, so the helper takes no
    # paths to resolve. It cannot be used as a file reader, and a request shaped
    # like the old path-based protocol is refused rather than interpreted.
    helper = Path(bundle_module.__file__).resolve().parent / bundle_module._WEB_ANALYSER_RELATIVE
    parser = Path(bundle_module.__file__).resolve().parent / bundle_module._ACORN_RELATIVE
    secret = tmp_path / "secret.js"
    secret.write_text("export const secret = 1;\n")

    for request in (
        {"root": str(tmp_path), "files": ["../secret.js"]},
        {"root": str(tmp_path), "files": ["/etc/hosts"]},
        {"files": [{"path": "a.js"}]},
        {"files": [{"path": "a.js", "source": None}]},
        {"files": [str(secret)]},
    ):
        completed = subprocess.run(
            [str(analyser_node), "--no-warnings", str(helper), str(parser)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode != 0, request
        assert "invalid" in completed.stderr, completed.stderr
        assert "secret" not in completed.stdout

    # The helper imports nothing that can read a file by path.
    assert "readFileSync(absolute" not in helper.read_text()
    assert 'from "node:fs"' in helper.read_text()  # only stdin (fd 0) is read


def test_analyser_helper_requires_an_absolute_parser_path(analyser_node: Path) -> None:
    helper = Path(bundle_module.__file__).resolve().parent / bundle_module._WEB_ANALYSER_RELATIVE
    completed = subprocess.run(
        [str(analyser_node), "--no-warnings", str(helper), "acorn.mjs"],
        input=json.dumps({"root": os.getcwd(), "files": []}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0
    assert "absolute acorn path" in completed.stderr


def test_analyser_ignores_a_hostile_caller_environment(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `NODE_OPTIONS` can preload code (`--import`) that rewrites the analyser's
    # report, which would make any payload verify clean. The analyser therefore
    # runs under a closed environment and inherits nothing from the caller.
    preload = tmp_path / "preload.mjs"
    preload.write_text(
        "const write = process.stdout.write.bind(process.stdout);\n"
        "process.stdout.write = (chunk, ...rest) => {\n"
        "  try {\n"
        "    const report = JSON.parse(chunk);\n"
        "    for (const entry of report.files ?? []) {\n"
        "      entry.references = [];\n"
        "      entry.nonLiteralReferences = [];\n"
        "      entry.stringLiterals = ['cortex-r0-build-1'];\n"
        "    }\n"
        "    return write(JSON.stringify(report), ...rest);\n"
        "  } catch {\n"
        "    return write(chunk, ...rest);\n"
        "  }\n"
        "};\n"
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--import {preload.as_uri()}")

    web_root, web_ledger = web_closure
    target = web_root / "server/index.js"
    target.write_text(target.read_text() + 'import evil from "evilnpmpkg";\n')
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)


@pytest.mark.parametrize(
    ("case", "script", "expected"),
    [
        ("non-zero-exit", "process.exit(3);\n", "analyser failed"),
        ("no-output", "", "output is malformed"),
        ("garbage-output", "process.stdout.write('not json');\n", "output is malformed"),
        (
            "truncated-json",
            "process.stdout.write('{\"files\": [');\n",
            "output is malformed",
        ),
        ("wrong-entry-count", "process.stdout.write('{\"files\": []}');\n", "output is malformed"),
        (
            "mismatched-path",
            "process.stdout.write(JSON.stringify({files: [{path: 'other.js', error: null,"
            " references: [], nonLiteralReferences: [], stringLiterals: [],"
            " adapterDeclarations: []}]}));\n",
            "output is malformed",
        ),
        (
            "missing-field",
            "process.stdout.write(JSON.stringify({files: [{path: 'a.js', error: null,"
            " references: [], nonLiteralReferences: [], stringLiterals: []}]}));\n",
            "output is malformed",
        ),
    ],
)
def test_analyser_fails_closed_on_every_unusable_report(
    tmp_path: Path,
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    script: str,
    expected: str,
) -> None:
    # Each of these is a branch a later refactor could silently invert into a
    # fail-open, so each one is pinned.
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_text("export const ready = true;\n")
    liar = tmp_path / "liar.mjs"
    liar.write_text(script or "process.exit(0);\n")
    monkeypatch.setattr(
        bundle_module, "_WEB_ANALYSER_RELATIVE", os.path.relpath(liar, Path(bundle_module.__file__).resolve().parent)
    )

    with pytest.raises(BundleVerificationError, match=expected):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


def test_analyser_fails_closed_when_the_report_times_out(
    tmp_path: Path,
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_text("export const ready = true;\n")
    slow = tmp_path / "slow.mjs"
    slow.write_text("await new Promise((resolve) => setTimeout(resolve, 30000));\n")
    monkeypatch.setattr(
        bundle_module, "_WEB_ANALYSER_RELATIVE", os.path.relpath(slow, Path(bundle_module.__file__).resolve().parent)
    )
    monkeypatch.setattr(bundle_module, "_WEB_ANALYSER_TIMEOUT", 1)

    with pytest.raises(BundleVerificationError, match="did not run"):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


def test_analyser_fails_closed_on_an_oversized_report(
    tmp_path: Path,
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_text("export const ready = true;\n")
    flood = tmp_path / "flood.mjs"
    flood.write_text("process.stdout.write('x'.repeat(4096));\n")
    monkeypatch.setattr(
        bundle_module, "_WEB_ANALYSER_RELATIVE", os.path.relpath(flood, Path(bundle_module.__file__).resolve().parent)
    )
    monkeypatch.setattr(bundle_module, "_WEB_ANALYSER_OUTPUT_LIMIT", 1024)

    with pytest.raises(BundleVerificationError, match="output is oversized"):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


@pytest.mark.parametrize(
    "builtin",
    ["node:child_process", "node:module", "node:vm", "node:worker_threads", "node:process"],
)
def test_analyser_rejects_node_builtins_outside_the_allowlist(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    builtin: str,
) -> None:
    # A `node:` grammar would admit precisely the builtins that defeat closure
    # analysis: `node:module` gives `createRequire(...)("child_process")`, and
    # `node:vm` loads code the parser never sees.
    web_root, web_ledger = web_closure
    target = web_root / "server/index.js"
    target.write_text(target.read_text() + f'import * as escape from "{builtin}";\nvoid escape;\n')
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure module reference"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)


@pytest.mark.parametrize(
    "content",
    [
        '{"private":true}\n',
        '{"private":true,"type":"commonjs"}\n',
        '{"private":true,"type":"module","imports":{"#x":"https://evil.example/x.js"}}\n',
        '{"private":true,"type":"module"}',
    ],
)
def test_analyser_pins_the_payload_package_metadata(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    content: str,
) -> None:
    # `package.json` decides how Node RESOLVES the payload's modules. Dropping
    # `"type":"module"` makes the server entry CommonJS, where `require(...)` is
    # invisible to an ESM closure analysis, so its bytes are pinned exactly.
    web_root, web_ledger = web_closure
    (web_root / "package.json").write_text(content)
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure package metadata"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)


@pytest.mark.parametrize(
    "rogue",
    ['"cortex-r0-build-EVI\\x4C"', '`cortex-r0-build-EVI\\x4C`', '"cortex-r0-build-" + "EVIL"'],
)
def test_analyser_sees_rogue_markers_hidden_by_escapes_or_concatenation(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    rogue: str,
) -> None:
    # The rogue-marker rule is a NEGATIVE assertion, so the literal set it scans
    # must not silently drop values: an escaped literal and a folded
    # concatenation both denote a real string at runtime.
    web_root, web_ledger = web_closure
    target = web_root / "server/index.js"
    target.write_text(target.read_text() + f"const spoof = {rogue};\nvoid spoof;\n")
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="build identifier"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)


def test_verifier_rejects_a_bundle_whose_shipped_tools_were_substituted(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # A bundle ships `tools/distribution/`, and an installed generation verifies
    # itself with that copy — so for the in-bundle verifier the pinned parser
    # digest is self-attestation. A verifier running from a checkout refuses any
    # bundle whose tools differ from its own, which is what closes the
    # substituted-parser forgery at the point a bundle is admitted.
    web_root, web_ledger = web_closure
    result = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)
    bundle = result.path

    liar = bundle / "tools/distribution/vendor/acorn-8.16.0.mjs"
    liar.write_text(
        "export function parse() {\n"
        "  return { type: 'Program', body: [], sourceType: 'module' };\n"
        "}\n"
    )
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="bundled distribution tools"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_verifier_rejects_a_bundle_missing_its_shipped_analyser(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    result = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)
    bundle = result.path

    (bundle / "tools/distribution/web_closure.mjs").unlink()
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="bundled distribution tools"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_verifier_rejects_a_bundle_whose_entrypoint_lies(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    # `tools/cortex_dist.py` and `cortex-dist` decide what code the bundle's own
    # verifier runs, so they are pinned alongside the library: a bundle that
    # simply prints a clean verdict must not pass a checkout-side verification.
    web_root, web_ledger = web_closure
    bundle = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node).path

    (bundle / "tools/cortex_dist.py").write_text('print(\'{"ok": true}\')\n')
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="bundled distribution tools"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_verifier_rejects_an_extra_file_under_tools(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    web_root, web_ledger = web_closure
    bundle = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node).path

    (bundle / "tools/distribution/extra_helper.py").write_text("# smuggled\n")
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="bundled distribution tools"):
        verify_bundle(bundle, node_executable=analyser_node)


def _replace_wheel_member(wheel: Path, relative: str, payload: bytes | None) -> None:
    """Rewrite one member of a wheel, or drop it, preserving the rest."""

    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        members = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    if payload is None:
        members.pop(relative, None)
    else:
        members[relative] = payload
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)


def test_verifier_rejects_a_wheel_whose_staging_kernel_was_substituted(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """⟦AMD-2b⟧'s residual, now that `cortex runtime stage` executes this copy.

    The kernel travels twice — in `tools/` for the installer, which runs under a
    host interpreter with only `tools/` on `sys.path`, and inside the cortex
    wheel for the installed product. Only the first copy was ever verified, and
    a bundle could therefore ship an installer that seals and probes correctly
    beside a product that does not.
    """

    web_root, web_ledger = web_closure
    bundle = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node).path

    wheel = next((bundle / "artifacts" / "wheels").glob("cortex-*.whl"))
    _replace_wheel_member(
        wheel,
        "cortex_platform/runtime_staging.py",
        b"def stage_python_runtime(*args, **kwargs):\n    return None\n",
    )
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="staging kernel differs"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_verifier_rejects_a_wheel_that_carries_no_staging_kernel(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """Absence is refused as loudly as substitution.

    An installed product without the kernel cannot stage a slot's interpreter at
    all, and finding that out when an operator runs `cortex runtime stage` is
    finding it out at the worst possible moment.
    """

    web_root, web_ledger = web_closure
    bundle = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node).path

    wheel = next((bundle / "artifacts" / "wheels").glob("cortex-*.whl"))
    _replace_wheel_member(wheel, "cortex_platform/runtime_staging.py", None)
    _rewrite_outer_checksums(bundle)

    with pytest.raises(BundleVerificationError, match="does not carry the staging kernel"):
        verify_bundle(bundle, node_executable=analyser_node)


def test_the_tools_and_wheel_staging_kernels_are_one_file(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
) -> None:
    """The positive half: three copies, one set of bytes.

    `_verify_bundled_tools` pins the `tools/` copy to the verifying checkout and
    the new gate pins the wheel's copy to the `tools/` copy, so a bundle that
    verifies from a checkout carries exactly that checkout's kernel in both
    places.
    """

    import zipfile

    web_root, web_ledger = web_closure
    bundle = _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node).path
    verify_bundle(bundle, node_executable=analyser_node)

    checkout = (
        Path(bundle_module.__file__).resolve().parent.parent
        / "cortex_platform"
        / "runtime_staging.py"
    ).read_bytes()
    shipped = (bundle / "tools" / "cortex_platform" / "runtime_staging.py").read_bytes()
    wheel = next((bundle / "artifacts" / "wheels").glob("cortex-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        packaged = archive.read("cortex_platform/runtime_staging.py")

    assert shipped == checkout
    assert packaged == checkout


@pytest.mark.parametrize("case", ["absent", "symlinked"])
def test_analysis_fails_closed_when_the_analyser_helper_is_unusable(
    tmp_path: Path,
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_text("export const ready = true;\n")
    source = Path(bundle_module.__file__).resolve().parent
    if case == "absent":
        candidate = tmp_path / "absent.mjs"
    else:
        candidate = tmp_path / "linked.mjs"
        candidate.symlink_to(source / bundle_module._WEB_ANALYSER_RELATIVE)
    monkeypatch.setattr(
        bundle_module, "_WEB_ANALYSER_RELATIVE", os.path.relpath(candidate, source)
    )

    with pytest.raises(BundleVerificationError, match="analyser is missing"):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("invalid-utf8", b"export const x = 1;\n\xff\xfe"),
        ("lone-surrogate", b"export const x = 1;\n\xed\xa0\x80"),
    ],
)
def test_analysis_fails_closed_on_undecodable_javascript(
    tmp_path: Path,
    analyser_node: Path,
    case: str,
    payload: bytes,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_bytes(payload)

    with pytest.raises(BundleVerificationError, match="unsafe Web closure text"):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


def test_analysis_fails_closed_on_oversized_javascript(
    tmp_path: Path,
    analyser_node: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "a.js").write_text("export const x = 1;\n" + "// pad\n" * 200)
    monkeypatch.setattr(bundle_module, "_WEB_SOURCE_SIZE_LIMIT", 64)

    with pytest.raises(BundleVerificationError, match="JavaScript is oversized"):
        bundle_module._analyse_web_javascript(analyser_node, root, ["a.js"])


@pytest.mark.parametrize(
    "rogue",
    [
        '`cortex-r0-build-${"EVIL"}`',
        '`cortex-r0-build-${"EV" + "IL"}`',
        '`${"cortex"}-r0-build-${"EVIL"}`',
    ],
)
def test_analyser_folds_literal_template_substitutions_for_marker_scanning(
    tmp_path: Path,
    wheel_pair: tuple[Path, Path],
    web_closure: tuple[Path, Path],
    analyser_node: Path,
    rogue: str,
) -> None:
    # A template whose every substitution is a literal is the same construct as
    # `"a" + "b"`; folding one and not the other would leave the rogue-marker
    # rule asymmetrically blind.
    web_root, web_ledger = web_closure
    target = web_root / "server/index.js"
    target.write_text(target.read_text() + f"const spoof = {rogue};\nvoid spoof;\n")
    _rewrite_web_ledger(web_root, web_ledger)

    with pytest.raises(BundleVerificationError, match="build identifier"):
        _compose(tmp_path / "bundle", wheel_pair, web_root, web_ledger, analyser_node)
