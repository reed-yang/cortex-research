"""The vendoring and staging runtime profiles must describe the same runtime.

S3.2 keeps two profile objects (design AMD-1b). `tools/vendor_python_runtime.py`
owns the vendoring policy — prune globs, residual allowlist, upstream asset —
and `cortex_platform/runtime_staging.py` owns the staging policy, because the
wheel cannot import the operator's tooling and the two content tables genuinely
differ: the vendor side requires `libpython` and four directories the staging
side does not.

Two objects can drift where one cannot, so the tables that MUST agree are pinned
here instead of trusted to review. `tests/packaging` is the one suite that can
import both sides.
"""

from __future__ import annotations

import pytest

from cortex_platform import runtime_staging
from tools import vendor_python_runtime as vendor

# Both profiles exist to produce and then expand one macOS arm64 artifact.
PLATFORM_TAG = "macosx_11_0_arm64"


def test_neither_side_carries_a_profile_the_other_lacks() -> None:
    assert set(vendor.PROFILES) == set(runtime_staging.PROFILES)
    assert vendor.PROFILES
    assert vendor.DEFAULT_PROFILE.name == runtime_staging.DEFAULT_PROFILE.name


@pytest.mark.parametrize("name", sorted(vendor.PROFILES))
def test_the_vendor_and_staging_profiles_agree(name: str) -> None:
    vendored = vendor.PROFILES[name]
    staged = runtime_staging.PROFILES[name]

    # The archive the tool emits and the tree the kernel expands are the same
    # runtime, so every path derived from the release must be one answer.
    assert vendored.release == staged.release
    assert vendored.interpreter_relative == staged.interpreter_relative
    assert vendored.library_relative == staged.library_relative
    assert vendored.library_id == staged.library_id

    # The surfaces the tool rewrites are the surfaces the kernel re-binds and
    # then asserts are residue-free. An entry on one side only is a silent hole:
    # an unpatched file the stager never looks at, or a patched file the vendor
    # never normalised.
    assert vendored.patch_set == staged.patch_set

    # What the tool prunes is exactly what the kernel refuses to find.
    assert vendored.forbidden_paths == staged.forbidden_paths


@pytest.mark.parametrize("name", sorted(vendor.PROFILES))
def test_both_profiles_target_the_same_platform(name: str) -> None:
    vendored = vendor.PROFILES[name]
    staged = runtime_staging.PROFILES[name]

    assert vendored.platform_tag == PLATFORM_TAG
    # The staging profile carries no platform tag of its own: its target is
    # spelled by the shapes it derives, so those are pinned against the tag the
    # vendor side stamps into the archive and pin names.
    major, minor = staged.supported_release
    assert vendored.abi_tag == staged.name == f"cp{major}{minor}"
    assert staged.extension_suffix == f".cpython-{major}{minor}-darwin.so"
    assert staged.library_relative.endswith(".dylib")
    assert staged.expected_rpaths == frozenset({"@executable_path/../lib"})
