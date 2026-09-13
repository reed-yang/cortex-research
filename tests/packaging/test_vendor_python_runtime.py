from __future__ import annotations

import base64
import hashlib
import json
import re
import tarfile
from pathlib import Path

import pytest

from tools import vendor_python_runtime as vendor

BUILD_ROOT = "/var/folders/1r/b8l6nzhj4zdbz5jmjdhqbprw0000gn/T/tmp_y66dptd"
PRIVATE_BUILD_ROOT = f"/private{BUILD_ROOT}"


def _write(path: Path, contents: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    return path


def _upstream_tree(root: Path) -> Path:
    """A miniature of the pinned artifact, carrying every measured hazard."""

    tree = root / "python"
    _write(tree / "bin/python3.14", b"\xcf\xfa\xed\xfe" + b"frozen getpath PREFIX /install\x00")
    (tree / "bin/python3.14").chmod(0o755)
    (tree / "bin/python3").symlink_to("python3.14")
    _write(tree / "bin/pip3", "#!/install/bin/python3.14\nprint('pip')\n")
    (tree / "bin/pip3").chmod(0o755)
    _write(tree / "lib/libpython3.14.dylib", b"\xcf\xfa\xed\xfe" + b"PREFIX /install\x00")
    _write(
        tree / "lib/python3.14/_sysconfigdata__darwin_darwin.py",
        "build_time_vars = {\n"
        f'    "AR": "{BUILD_ROOT}/tools/llvm/bin/llvm-ar",\n'
        '    "BINDIR": "/install/bin",\n'
        "    \"CONFIG_ARGS\": \"'--prefix=/install' "
        f"'--with-openssl={BUILD_ROOT}/tools/deps'\",\n"
        '    "INCLUDEPY": "/install/include/python3.14",\n'
        '    "LIBDIR": "/install/lib",\n'
        f'    "abs_srcdir": "{PRIVATE_BUILD_ROOT}/Python-3.14.6",\n'
        "}\n",
    )
    _write(
        tree / "lib/python3.14/_sysconfig_vars__darwin_darwin.json",
        json.dumps({"BINDIR": "/install/bin", "AR": f"{BUILD_ROOT}/tools/llvm/bin/llvm-ar"}),
    )
    _write(
        tree / "lib/python3.14/config-3.14-darwin/Makefile",
        "prefix=\t\t/install\n"
        f"CONFIG_ARGS=\t '--prefix=/install' '--with-openssl={BUILD_ROOT}/tools/deps'\n"
        "\t$(INSTALL_SCRIPT) $(srcdir)/install-sh $(DESTDIR)$(LIBPL)/install-sh\n",
    )
    _write(
        tree / "lib/python3.14/config-3.14-darwin/python.o",
        b"\xcf\xfa\xed\xfe" + BUILD_ROOT.encode(),
    )
    _write(tree / "lib/pkgconfig/python-3.14.pc", "prefix=/install\nlibdir=${prefix}/lib\n")
    (tree / "lib/pkgconfig/python3.pc").symlink_to("python-3.14.pc")
    _write(tree / "lib/python3.14/os.py", "sep = '/'\n")
    _write(tree / "lib/python3.14/venv/__init__.py", "")
    _write(tree / "lib/python3.14/ensurepip/__init__.py", "")
    _write(tree / "lib/python3.14/LICENSE.txt", "PSF\n")
    _write(tree / "lib/python3.14/site-packages/pip/__init__.py", "")
    _write(
        tree / "lib/python3.14/site-packages/pip/_internal/models/scheme.py",
        "# https://docs.python.org/3/install/index.html#alternate-installation\n",
    )
    _write(
        tree / "lib/python3.14/site-packages/pip-26.1.2.dist-info/direct_url.json",
        json.dumps({"url": f"file://{PRIVATE_BUILD_ROOT}/pip-26.1.2-py3-none-any.whl"}),
    )
    _write(tree / "include/python3.14/Python.h", "/* header */\n")

    # Everything below is removed by the frozen prune policy.
    _write(tree / "lib/python3.14/idlelib/idle.py", "")
    _write(tree / "lib/python3.14/tkinter/__init__.py", "")
    _write(tree / "lib/python3.14/lib-dynload/_tkinter.cpython-314-darwin.so", b"\xcf\xfa\xed\xfe")
    _write(tree / "lib/python3.14/lib-dynload/_dbm.cpython-314-darwin.so", b"\xcf\xfa\xed\xfe")
    _write(tree / "lib/libtcl9.0.dylib", b"\xcf\xfa\xed\xfe" + BUILD_ROOT.encode())
    _write(tree / "lib/libtcl9tk9.0.dylib", b"\xcf\xfa\xed\xfe" + BUILD_ROOT.encode())
    _write(tree / "lib/tcl9/init.tcl", "")
    _write(tree / "lib/tcl9.0/init.tcl", "")
    _write(tree / "lib/tk9.0/tk.tcl", "")
    _write(tree / "lib/itcl4.3.5/itclConfig.sh", f"ITCL_BUILD_LIB_SPEC='{BUILD_ROOT}/lib'\n")
    _write(tree / "lib/itcl4.3.5/libitclstub.a", b"!<arch>\n")
    _write(tree / "lib/thread3.0.4/libthread3.0.4.dylib", b"\xcf\xfa\xed\xfe")
    _write(tree / "lib/python3.14/__pycache__/os.cpython-314.pyc", b"\x00\x0f/install\x00")
    return tree


def _licences() -> dict[str, bytes]:
    return {name: f"{name} text\n".encode() for name in vendor.LICENCE_DOCUMENTS}


def _rpath_runner(*_command: str) -> str:
    return "@rpath/libpython3.14.dylib"


def _normalized(root: Path) -> Path:
    tree = _upstream_tree(root)
    vendor.normalize_tree(tree, licences=_licences(), run=_rpath_runner)
    return tree


def test_prune_removes_the_frozen_policy_set_and_keeps_the_required_content(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)

    vendor.prune_tree(tree)

    assert not (tree / "lib/python3.14/idlelib").exists()
    assert not (tree / "lib/python3.14/tkinter").exists()
    assert not (tree / "lib/python3.14/lib-dynload/_tkinter.cpython-314-darwin.so").exists()
    assert not (tree / "lib/python3.14/lib-dynload/_dbm.cpython-314-darwin.so").exists()
    assert not (tree / "lib/libtcl9.0.dylib").exists()
    assert not (tree / "lib/libtcl9tk9.0.dylib").exists()
    assert not (tree / "lib/itcl4.3.5").exists()
    assert not (tree / "lib/thread3.0.4").exists()
    assert not (tree / "lib/python3.14/__pycache__").exists()
    assert (tree / "lib/python3.14/ensurepip/__init__.py").is_file()
    assert (tree / "lib/python3.14/config-3.14-darwin/Makefile").is_file()
    assert (tree / "lib/python3.14/site-packages/pip/__init__.py").is_file()
    assert (tree / "lib/python3.14/LICENSE.txt").is_file()
    assert (tree / "lib/libpython3.14.dylib").is_file()
    assert (tree / "include/python3.14/Python.h").is_file()
    assert (tree / "lib/pkgconfig/python-3.14.pc").is_file()


def test_prune_covers_the_versioned_tcl_and_tk_directories(tmp_path: Path) -> None:
    """The frozen list named `lib/tcl9/`; the artifact also ships `lib/tcl9.0/`."""

    tree = _upstream_tree(tmp_path)

    vendor.prune_tree(tree)

    assert not (tree / "lib/tcl9").exists()
    assert not (tree / "lib/tcl9.0").exists()
    assert not (tree / "lib/tk9.0").exists()


def test_symlinks_are_materialized_into_regular_files(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)

    vendor.materialize_symlinks(tree)

    published = tree / "bin/python3"
    assert not published.is_symlink()
    assert published.read_bytes() == (tree / "bin/python3.14").read_bytes()
    assert not (tree / "lib/pkgconfig/python3.pc").is_symlink()
    assert not any(path.is_symlink() for path in tree.rglob("*"))


def test_a_symlink_escaping_the_tree_is_refused(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("escaped\n")
    (tree / "bin/escape").symlink_to(outside)

    with pytest.raises(vendor.VendorError, match="symlink"):
        vendor.materialize_symlinks(tree)


def test_build_host_paths_are_patched_across_the_whole_patch_set(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)
    vendor.prune_tree(tree)
    vendor.materialize_symlinks(tree)

    patched = vendor.patch_build_host_paths(tree)

    assert set(patched) == {
        "lib/pkgconfig/python-3.14.pc",
        "lib/pkgconfig/python3.pc",
        "lib/python3.14/_sysconfig_vars__darwin_darwin.json",
        "lib/python3.14/_sysconfigdata__darwin_darwin.py",
        "lib/python3.14/config-3.14-darwin/Makefile",
    }
    # `/install` as a path token; `install-sh` in the config Makefile is a
    # filename, not a build-host path, and is deliberately left alone.
    install_path = re.compile(r"/install(?![A-Za-z0-9_.-])")
    for relative in patched:
        text = (tree / relative).read_text()
        assert install_path.search(text) is None
        assert "/var/folders" not in text
    data = (tree / "lib/python3.14/_sysconfigdata__darwin_darwin.py").read_text()
    assert f'"BINDIR": "{vendor.PREFIX_MARKER}/bin"' in data
    assert vendor.BUILD_MARKER in data


def test_config_args_is_cleared_in_every_patched_surface(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)
    vendor.prune_tree(tree)
    vendor.materialize_symlinks(tree)

    vendor.patch_build_host_paths(tree)

    data = (tree / "lib/python3.14/_sysconfigdata__darwin_darwin.py").read_text()
    assert '"CONFIG_ARGS": ""' in data
    makefile = (tree / "lib/python3.14/config-3.14-darwin/Makefile").read_text()
    assert "CONFIG_ARGS=\n" in makefile
    assert "install-sh" in makefile


def test_an_inert_install_string_outside_the_patch_set_is_tolerated(tmp_path: Path) -> None:
    """`bin/python3.14` embeds `/install` as frozen getpath's PREFIX constant."""

    tree = _normalized(tmp_path)

    assert b"/install" in (tree / "bin/python3.14").read_bytes()
    vendor.assert_no_residual_build_paths(tree)


def test_a_residual_build_host_path_outside_the_allowlist_aborts(tmp_path: Path) -> None:
    tree = _normalized(tmp_path)
    _write(tree / "lib/python3.14/leaked.py", f"BUILD = '{BUILD_ROOT}/tools'\n")

    with pytest.raises(vendor.VendorError, match="build-host path"):
        vendor.assert_no_residual_build_paths(tree)


def test_the_measured_residual_allowlist_is_accepted(tmp_path: Path) -> None:
    tree = _normalized(tmp_path)

    leaked = tree / "lib/python3.14/config-3.14-darwin/python.o"
    provenance = tree / "lib/python3.14/site-packages/pip-26.1.2.dist-info/direct_url.json"
    assert BUILD_ROOT.encode() in leaked.read_bytes()
    assert BUILD_ROOT in provenance.read_text()
    vendor.assert_no_residual_build_paths(tree)


def test_licences_are_vendored_under_share_licenses(tmp_path: Path) -> None:
    tree = _normalized(tmp_path)

    root = tree / "share/licenses/python-build-standalone"
    assert (root / "LICENSE.openssl-3.txt").is_file()
    assert (root / "python-licenses.rst").is_file()
    assert {path.name for path in root.iterdir()} == set(vendor.LICENCE_DOCUMENTS)


def test_missing_required_content_after_normalization_aborts(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)
    (tree / "lib/python3.14/ensurepip/__init__.py").unlink()
    (tree / "lib/python3.14/ensurepip").rmdir()

    with pytest.raises(vendor.VendorError, match="content"):
        vendor.normalize_tree(tree, licences=_licences(), run=_rpath_runner)


def test_an_unexpected_dylib_id_is_repaired_and_reverified(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(*command: str) -> str:
        calls.append(command)
        if command[0].endswith("otool"):
            if len(calls) == 1:
                return "/absolute/libpython3.14.dylib"
            return "@rpath/libpython3.14.dylib"
        return ""

    vendor.normalize_dylib_id(tree, run=run)

    assert any(command[0].endswith("install_name_tool") for command in calls)


def test_a_dylib_id_that_cannot_be_repaired_aborts(tmp_path: Path) -> None:
    tree = _upstream_tree(tmp_path)

    with pytest.raises(vendor.VendorError, match="dylib id"):
        vendor.normalize_dylib_id(tree, run=lambda *_: "/absolute/libpython3.14.dylib")


def test_emitted_archive_is_byte_identical_on_re_emission(tmp_path: Path) -> None:
    first_tree = _normalized(tmp_path / "one")
    second_tree = _normalized(tmp_path / "two")

    first = vendor.emit_archive(first_tree, tmp_path / "first.tar.gz")
    second = vendor.emit_archive(second_tree, tmp_path / "second.tar.gz")

    assert (tmp_path / "first.tar.gz").read_bytes() == (tmp_path / "second.tar.gz").read_bytes()
    assert first["sha256"] == second["sha256"]
    assert first["size"] == (tmp_path / "first.tar.gz").stat().st_size


def test_emitted_archive_carries_only_normalized_entries(tmp_path: Path) -> None:
    tree = _normalized(tmp_path)

    vendor.emit_archive(tree, tmp_path / "runtime.tar.gz")

    with tarfile.open(tmp_path / "runtime.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
    assert members
    names = [member.name for member in members]
    assert names == sorted(names)
    assert "bin/python3.14" in names
    assert not any(name.startswith("python/") for name in names)
    for member in members:
        assert member.isdir() or member.isreg()
        assert member.uid == member.gid == 0
        assert member.uname == member.gname == ""
        assert member.mtime == 0
        assert member.mode in {0o755, 0o644}
    modes = {member.name: member.mode for member in members}
    assert modes["bin/python3.14"] == 0o755
    assert modes["bin/pip3"] == 0o755
    assert modes["lib/python3.14/os.py"] == 0o644


def _attestation_document(
    *,
    digest: str,
    rekor_names: tuple[str, ...],
    plain_names: tuple[str, ...],
) -> bytes:
    def bundle(names: tuple[str, ...], *, rekor: bool) -> dict[str, object]:
        statement = {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": name, "digest": {"sha256": digest}} for name in names],
        }
        material: dict[str, object] = {"certificate": {"rawBytes": "cert"}}
        if rekor:
            material["tlogEntries"] = [{"logIndex": "1"}]
        return {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": material,
            "dsseEnvelope": {
                "payloadType": "application/vnd.in-toto+json",
                "payload": base64.b64encode(json.dumps(statement).encode()).decode(),
            },
        }

    attestations = [{"bundle": bundle(rekor_names, rekor=True)}] if rekor_names else []
    if plain_names:
        attestations.append({"bundle": bundle(plain_names, rekor=False)})
    return json.dumps({"attestations": attestations}).encode()


def test_attestation_selection_matches_on_digest_not_on_the_release_asset_name() -> None:
    digest = "a" * 64
    document = _attestation_document(
        digest=digest,
        rekor_names=(
            "cpython-3.14.6-aarch64-apple-darwin-install_only_stripped-20260723T0928.tar.gz",
        ),
        plain_names=(vendor.UPSTREAM_ASSET,),
    )

    selected = vendor.select_attestations(document, digest=digest)

    payload = json.loads(selected)
    assert payload["digest"] == digest
    assert len(payload["bundles"]) == 2


def test_attestation_selection_refuses_without_a_rekor_backed_bundle() -> None:
    digest = "b" * 64
    document = _attestation_document(
        digest=digest,
        rekor_names=(),
        plain_names=(vendor.UPSTREAM_ASSET,),
    )

    with pytest.raises(vendor.VendorError, match="Rekor"):
        vendor.select_attestations(document, digest=digest)


def test_attestation_selection_refuses_when_nothing_binds_the_digest() -> None:
    document = _attestation_document(
        digest="c" * 64,
        rekor_names=("other.tar.gz",),
        plain_names=(),
    )

    with pytest.raises(vendor.VendorError, match="binds"):
        vendor.select_attestations(document, digest="d" * 64)


def test_pin_descriptor_is_closed_and_records_both_independent_digests(tmp_path: Path) -> None:
    tree = _normalized(tmp_path)
    archive = vendor.emit_archive(tree, tmp_path / "runtime.tar.gz")

    pin = vendor.build_pin(archive=archive, attestation_sha256="e" * 64)

    assert set(pin) == {
        "schema_version",
        "implementation",
        "version",
        "abi_tag",
        "platform_tag",
        "interpreter_path",
        "upstream",
        "normalization",
        "archive",
    }
    assert set(pin["upstream"]) == {
        "project",
        "release_tag",
        "asset",
        "sha256",
        "attestation_sha256",
    }
    assert set(pin["normalization"]) == {
        "normalizer_version",
        "sysconfig_prefix_patched",
        "pruned",
    }
    assert set(pin["archive"]) == {"name", "size", "sha256"}
    assert pin["upstream"]["sha256"] == vendor.UPSTREAM_ASSET_SHA256
    assert pin["archive"]["sha256"] == archive["sha256"]
    assert pin["normalization"]["pruned"] == list(vendor.PRUNE_POLICY)
    assert pin["interpreter_path"] == "bin/python3.14"


def test_asset_verification_requires_the_literal_pin_not_only_the_release_sums(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "asset.tar.gz"
    asset.write_bytes(b"not the pinned asset")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    sums = f"{digest}  {vendor.UPSTREAM_ASSET}\n"

    with pytest.raises(vendor.VendorError, match="digest"):
        vendor.verify_asset(asset, sha256sums=sums)


def test_asset_verification_refuses_a_release_sums_file_that_omits_the_asset(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "asset.tar.gz"
    asset.write_bytes(b"not the pinned asset")

    with pytest.raises(vendor.VendorError, match="SHA256SUMS"):
        vendor.verify_asset(asset, sha256sums="deadbeef  some-other-asset.tar.gz\n")
