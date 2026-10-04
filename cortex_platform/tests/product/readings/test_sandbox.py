"""The publisher's in-child no-delete verification must query the real policy."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from cortex_platform.product.readings.sandbox import ReadingsBoundaryError, verify_no_delete

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS publication sandbox')

# Runs in a disposable child because a sandbox cannot be removed once applied.
_VERIFY_UNDER_NO_DELETE_PROFILE = '''
import json, sys
from pathlib import Path
from cortex_platform.product.readings.sandbox import (
    ReadingsBoundaryError, apply_profile, no_delete_rules, verify_no_delete)
protected, unprotected = Path(sys.argv[1]), Path(sys.argv[2])
apply_profile('\\n'.join(['(version 1)', '(allow default)', *no_delete_rules(protected)]))
results = {}
for name, root in (('protected', protected), ('unprotected', unprotected)):
    try:
        verify_no_delete(root)
        results[name] = 'verified'
    except ReadingsBoundaryError as error:
        results[name] = str(error)
print(json.dumps(results))
'''


def test_no_delete_verification_answers_for_the_queried_root(tmp_path):
    # Siblings share every ancestor, so only the root entry itself differs.
    protected = tmp_path.resolve() / 'library' / 'papers'
    unprotected = tmp_path.resolve() / 'library' / 'unprotected'
    protected.mkdir(parents=True)
    unprotected.mkdir()
    process = subprocess.run(
        [sys.executable, '-I', '-B', '-c', _VERIFY_UNDER_NO_DELETE_PROFILE, str(protected), str(unprotected)],
        capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {
        'protected': 'verified',
        'unprotected': 'readings no-delete policy could not be verified',
    }


def test_no_delete_verification_fails_closed_without_a_sandbox(tmp_path):
    with pytest.raises(ReadingsBoundaryError, match='could not be verified'):
        verify_no_delete(tmp_path.resolve())
