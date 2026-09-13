"""Standard-library-only font closure shared by bundle assembly and verification."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

KATEX_MANIFEST = "licenses/katex-fonts.json"
KATEX_LICENSE = "licenses/MIT-KaTeX.txt"
# Regenerated alongside apps/web/scripts/katex-fonts.json when KaTeX is upgraded.
KATEX_MANIFEST_SHA256 = 'f6e832f6fa7834ff72484ab9c40a4ccada9928e5b6e9b7cc7333d480c56717cf'
KATEX_ASSET = re.compile(r"client/assets/(KaTeX_[A-Za-z0-9]+-[A-Za-z]+)-[A-Za-z0-9_-]{8,12}\.(ttf|woff|woff2)")


class WebFontError(ValueError):
    pass


def katex_manifest(files, prefix=""):
    path = files.get(prefix + KATEX_MANIFEST)
    if path is None:
        if prefix + KATEX_LICENSE in files or any(
            key.startswith(prefix) and KATEX_ASSET.fullmatch(key[len(prefix):]) for key in files
        ):
            raise WebFontError("KaTeX font manifest is missing")
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != KATEX_MANIFEST_SHA256:
        raise WebFontError("KaTeX font manifest does not match the reviewed version")
    manifest = json.loads(data)
    license_path = files.get(prefix + KATEX_LICENSE)
    if license_path is None or hashlib.sha256(license_path.read_bytes()).hexdigest() != manifest["license_sha256"]:
        raise WebFontError("KaTeX MIT license does not match")
    return manifest


def validate_fonts(entries, files, prefix, texts, geist):
    manifest = katex_manifest(files, prefix)
    fonts = manifest["fonts"] if manifest is not None else {}
    observed = []
    for relative in entries:
        match = KATEX_ASSET.fullmatch(relative)
        if match is None:
            continue
        expected = fonts.get(f"{match[1]}.{match[2]}")
        measured = hashlib.sha256(files[prefix + relative].read_bytes()).hexdigest()
        if expected is None or measured != expected:
            raise WebFontError("unsafe Web closure KaTeX font component")
        observed.append(measured)
    css = "\n".join(text for path, text in texts.items() if path.endswith(".css"))
    try:
        for encoded in re.findall(r"data:font/(?:ttf|woff|woff2);base64,([A-Za-z0-9+/]+={0,2})", css):
            decoded = base64.b64decode(encoded, validate=True)
            if base64.b64encode(decoded).decode("ascii") != encoded:
                raise WebFontError("unsafe Web closure font encoding")
            observed.append(hashlib.sha256(decoded).hexdigest())
    except (binascii.Error, ValueError) as exc:
        raise WebFontError("unsafe Web closure font encoding") from exc
    if sorted(observed) != sorted([*geist, *fonts.values()]):
        raise WebFontError("unsafe Web closure font components")
    for reference in re.findall(r"url\(\s*/assets/([A-Za-z0-9._-]+)\s*\)", css):
        if "client/assets/" + reference not in entries:
            raise WebFontError("unsafe Web closure missing CSS asset")
    notices = texts["THIRD_PARTY_NOTICES.md"]
    if any(digest not in notices or source not in notices for digest, (_, source) in geist.items()):
        raise WebFontError("unsafe Web closure third-party notice")
    if manifest is not None and (
        manifest["version"] not in notices or manifest["upstream"] not in notices
        or any(digest not in notices for digest in fonts.values())
    ):
        raise WebFontError("unsafe Web closure KaTeX notice")
    license_text = texts["licenses/OFL-1.1.txt"]
    if "Copyright 2024 The Geist Project Authors" not in license_text or "SIL OPEN FONT LICENSE Version 1.1" not in license_text:
        raise WebFontError("unsafe Web closure OFL terms")


def katex_supply(files, prefix=""):
    """Keep old bundles unchanged; derive new metadata from one pinned manifest."""
    manifest = katex_manifest(files, prefix)
    if manifest is None:
        return [], {}
    components = [{
        "type": "file", "name": "KaTeX font manifest", "version": manifest["version"],
        "hashes": [{"alg": "SHA-256", "content": KATEX_MANIFEST_SHA256}],
        "licenses": [{"license": {"id": "MIT"}}],
        "externalReferences": [{"type": "distribution", "url": manifest["upstream"]}],
    }, {
        "type": "file", "name": "KaTeX MIT license terms",
        "hashes": [{"alg": "SHA-256", "content": manifest["license_sha256"]}],
    }]
    components.extend({
        "type": "file", "name": name,
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "licenses": [{"license": {"id": "MIT"}}],
    } for name, digest in sorted(manifest["fonts"].items()))
    return components, {"katex": {
        "version": manifest["version"], "manifest_sha256": KATEX_MANIFEST_SHA256,
        "license_sha256": manifest["license_sha256"],
        "font_components": sorted(manifest["fonts"].values()),
    }}
