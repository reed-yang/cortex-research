"""KaTeX reaches the independent bundle gate without changing old generations."""

import base64
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from distribution.bundle import BundleBuilder, BundleVerificationError, verify_bundle
from distribution.web_fonts import KATEX_LICENSE, KATEX_MANIFEST, KATEX_MANIFEST_SHA256

REPOSITORY = Path(__file__).resolve().parents[2]


def ledger(root, path):
    rows = []
    for entry in sorted(p for p in root.rglob("*") if p.is_file()):
        data = entry.read_bytes()
        rows.append(f"{hashlib.sha256(data).hexdigest()}  {len(data)}  {entry.relative_to(root).as_posix()}")
    path.write_text("\n".join(rows) + "\n")


@pytest.fixture
def math_closure(web_closure):
    root, listing = web_closure
    font_root = REPOSITORY / "apps/web/node_modules/katex/dist/fonts"
    if not font_root.is_dir():
        pytest.skip("KaTeX bundle acceptance requires apps/web npm ci")
    data = (REPOSITORY / "apps/web/scripts/katex-fonts.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == KATEX_MANIFEST_SHA256
    manifest = json.loads(data)
    (root / KATEX_MANIFEST).write_bytes(data)
    shutil.copyfile(REPOSITORY / "apps/web" / KATEX_LICENSE, root / KATEX_LICENSE)
    css = root / "client/assets/app.css"
    rules = []
    for index, name in enumerate(manifest["fonts"]):
        data = (font_root / name).read_bytes()
        if index == 0:
            url = f"data:font/{Path(name).suffix[1:]};base64,{base64.b64encode(data).decode()}"
        else:
            hashed = f"{Path(name).stem}-abcdefgh{Path(name).suffix}"
            (root / "client/assets" / hashed).write_bytes(data)
            url = f"/assets/{hashed}"
        rules.append(f"@font-face {{ src: url({url}); }}")
    css.write_text(css.read_text() + "\n" + "\n".join(rules))
    ledger(root, listing)
    return root, listing


def assemble(tmp_path, wheel_pair, analyser_node, closure):
    root, listing = closure
    return BundleBuilder(tmp_path / "bundle").assemble(
        release_id="cortex-dev-2", release_sequence=2, source_commit="1" * 40,
        lock_sha256="2" * 64, wheels=wheel_pair, created_at="2026-07-23T12:00:00Z",
        web_payload_root=root, web_payload_ledger=listing, web_build_id="cortex-r0-build-1",
        web_lock_sha256="3" * 64, node_adapter_version=1, node_executable=analyser_node,
    )


def test_math_bundle_accounts_for_files_embedded_fonts_and_mit(tmp_path, wheel_pair, analyser_node, math_closure):
    result = assemble(tmp_path, wheel_pair, analyser_node, math_closure)
    checked = verify_bundle(result.path, node_executable=analyser_node)
    evidence = checked.provenance["web_build_inputs"]["katex"]
    assert evidence["version"] == "0.16.47" and len(evidence["font_components"]) == 60
    sbom = json.loads((result.path / "sbom.cdx.json").read_text())
    assert any(entry["name"] == "KaTeX MIT license terms" for entry in sbom["components"])
    assert sum(entry["name"].startswith("KaTeX_") for entry in sbom["components"]) == 60


def test_legacy_bundle_still_has_its_original_font_metadata(tmp_path, wheel_pair, analyser_node, web_closure):
    result = assemble(tmp_path, wheel_pair, analyser_node, web_closure)
    checked = verify_bundle(result.path, node_executable=analyser_node)
    assert "katex" not in checked.provenance["web_build_inputs"]
    assert len(checked.provenance["web_build_inputs"]["font_components"]) == 2


@pytest.mark.parametrize("fault", ["font", "missing", "duplicate", "manifest", "license", "reference"])
def test_rewritten_math_ledger_cannot_hide_unapproved_fonts(tmp_path, wheel_pair, analyser_node, math_closure, fault):
    root, listing = math_closure
    font = next((root / "client/assets").glob("*.woff2"))
    if fault == "font":
        font.write_bytes(font.read_bytes() + b"changed")
    elif fault == "missing":
        font.unlink()
    elif fault == "duplicate":
        shutil.copyfile(font, font.with_name(font.name.replace("abcdefgh", "ijklmnop")))
    elif fault == "manifest":
        (root / KATEX_MANIFEST).write_text("{}")
    elif fault == "license":
        (root / KATEX_LICENSE).write_text("not the license")
    else:
        css = root / "client/assets/app.css"
        css.write_text(css.read_text() + "\na { background: url(/assets/missing.woff2); }")
    ledger(root, listing)
    with pytest.raises(BundleVerificationError):
        assemble(tmp_path, wheel_pair, analyser_node, math_closure)
