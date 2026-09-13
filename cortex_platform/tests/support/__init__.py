"""Test-only adapters shared by Python tests and the Web process fixtures.

These helpers used to live inside the shipped packages (`product/sources` and
`product/workflows`). They are test doubles, so they belong under `tests/`,
which the wheel excludes (`pyproject.toml` `**/tests/**`).
"""
