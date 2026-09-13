"""Package marker.

Without it `tests/private_access/conftest.py` claims the bare module name
`conftest`. Making the directory a package moves this conftest to
`private_access.conftest` and leaves the top-level name to the `tests/` one.

It did not, on its own, fix the collection errors a564a9f's commit message
claims it fixed: `tests/distribution/conftest.py` has exactly the same shape and
no package marker, and `pytest tests` reported 17 errors both immediately before
and immediately after this file was added. What fixed them is
`tests/approval_gate.py`, a module name no `conftest.py` can claim. The
symmetrical move here — `tests/distribution/__init__.py` — is not available:
`tests.distribution` would shadow the repository's real top-level
`distribution/` package (bundle.py, wheel_closure.py, lifecycle.py) and take
`pytest tests/distribution` from 728 collected to zero.
"""
