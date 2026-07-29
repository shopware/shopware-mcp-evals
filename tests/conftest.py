"""Shared pytest configuration.

Deliberately empty of import plumbing. The repo is an installed package
(`pip install -e .`), so tests import their subject directly —
`from eval import runner`, `from functional.reporting import Reporter`. This
file used to prepend the repo root and functional/ to sys.path, which every
test module then had to work around: six of them loaded their subject with
importlib.util.spec_from_file_location because two modules were both named
`run` and the import would land on whichever came first on the path.
"""
