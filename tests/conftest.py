"""Shared pytest configuration.

Deliberately empty of import plumbing. The repo is an installed package
(`pip install -e .`), so tests import their subject directly —
`from eval import runner`, `from functional.reporting import Reporter`. This
file used to prepend the repo root and functional/ to sys.path, which every
test module then had to work around: six of them loaded their subject with
importlib.util.spec_from_file_location because two modules were both named
`run` and the import would land on whichever came first on the path.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_developer_shop(monkeypatch):
    """Point every test at a lane that cannot exist.

    These are offline tests, but "offline" was an intention rather than a fact:
    `SW_BASE_URL` is read from the environment, so on a machine with a running
    instance the suite quietly talked to it. That is how a startup test which
    calls the real placeholder resolvers passed locally for a developer with a
    shop on :8100 and failed in CI with `Connection refused` — the test was
    green because of a server nobody meant to involve.

    Blocking sockets outright would be stricter, and would also be a much bigger
    change: several tests fake `mcp_call` at the module level rather than at the
    transport, so the boundary they exercise is above this. Removing the
    accidental dependency is the part that was actually wrong.

    Port 0 is not connectable, so anything that slips through fails fast and
    locally rather than reaching a real shop.
    """
    monkeypatch.setenv("SW_BASE_URL", "http://localhost:0")
