"""Marks tests/ as a package so `from tests.stubs import ...` resolves.

Without this file pytest inserts `tests/` itself into sys.path and the repo root
never goes on it, so a shared helper module is importable under
`python -m pytest` (which adds the CWD) and not under plain `pytest` — which is
what CI runs. The suite collected fine locally and failed in CI with
`ModuleNotFoundError: No module named 'tests'` on 14 modules at once.

Nothing else belongs here: the tests import the source modules from the
installed package, which is the arrangement pyproject.toml describes.
"""
