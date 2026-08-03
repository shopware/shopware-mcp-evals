"""Shared pytest configuration.

Deliberately empty of import plumbing. The repo is an installed package
(`pip install -e .`), so tests import their subject directly —
`from eval import runner`, `from functional.reporting import Reporter`. This
file used to prepend the repo root and functional/ to sys.path, which every
test module then had to work around: six of them loaded their subject with
importlib.util.spec_from_file_location because two modules were both named
`run` and the import would land on whichever came first on the path.
"""

from urllib.parse import urlparse

import pytest

import mcp_client

# Not connectable, and obviously deliberate if it shows up in a failure message.
DEAD_LANE = "http://localhost:0"


@pytest.fixture(autouse=True)
def _no_developer_shop(monkeypatch):
    """Point every test at a lane that cannot exist.

    These are offline tests, but "offline" was an intention rather than a fact:
    `SW_BASE_URL` is read from the environment, so on a machine with a running
    instance the suite quietly talked to it. That is how a startup test which
    calls the real placeholder resolvers passed locally for a developer with a
    shop on :8100 and failed in CI with `Connection refused` — green because of
    a server nobody meant to involve.

    Setting the env var alone does NOT do this, which was the first attempt and
    was pure theatre: `mcp_client.SW_BASE_URL` and the ADMIN/STORE endpoints are
    built at import, so by the time a fixture runs the value is already captured.
    Verified: with SW_BASE_URL=http://localhost:8100 and only setenv patched,
    `ADMIN.url` was still `http://localhost:8100/api/_mcp`. The module constant
    and each endpoint's url have to be patched too.

    Not a socket block, which would be stricter — several tests fake `mcp_call`
    at module level, so the boundary they exercise sits above the transport.
    Removing the accidental dependency is the part that was actually wrong.
    """
    monkeypatch.setenv("SW_BASE_URL", DEAD_LANE)
    monkeypatch.setattr(mcp_client, "SW_BASE_URL", DEAD_LANE)
    monkeypatch.setattr(mcp_client, "MCP_URL", f"{DEAD_LANE}/api/_mcp")
    for endpoint in (mcp_client.ADMIN, mcp_client.STORE):
        monkeypatch.setattr(endpoint, "url", f"{DEAD_LANE}{urlparse(endpoint.url).path}")
