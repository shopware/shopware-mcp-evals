"""The catalogue snapshot.

This writes tool-history/latest.json, which the drift check diffs and which
tests/test_ownership.py and tests/test_fixtures.py assert against — so its shape
is a contract, and it was the one module at 0% coverage.

Normalisation is the part worth pinning: everything is sorted and session state
is dropped, because otherwise a `git diff` between two snapshots reports
reordering and enablement as description churn.
"""

import json

import pytest

from eval import snapshot_tools as S


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    for name in ("SW_BASE_URL", "SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY"):
        monkeypatch.setattr(S, name, "set")


def wire(monkeypatch, *, default_tools=None, toolsets=None, full=None, instructions="Server says hi"):
    """Stand in for a live server. `default_tools` is the fresh-session surface;
    `full` is what tools/list returns once every toolset is enabled."""
    calls = {"enabled": 0}
    pages = [default_tools if default_tools is not None else [], full if full is not None else []]

    def tools_list_all(_sid, endpoint=None):
        # First call is the default surface, second is post-enable.
        return pages[min(calls["enabled"], 1)]

    def enable_all(_sid, endpoint=None):
        calls["enabled"] += 1
        return []

    monkeypatch.setattr(S, "mcp_init", lambda endpoint=None: ("sid", instructions))
    monkeypatch.setattr(S, "mcp_tools_list_all", tools_list_all)
    monkeypatch.setattr(S, "mcp_toolsets_list", lambda _sid, endpoint=None: toolsets or [])
    monkeypatch.setattr(S, "enable_all_toolsets", enable_all)
    return calls


def run(monkeypatch, tmp_path, **kw):
    out = tmp_path / "snap" / "latest.json"
    monkeypatch.setattr("sys.argv", ["snapshot_tools", "--output", str(out)])
    code = S.main()
    return code, out


def test_missing_credentials_exit_one_without_touching_the_network(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(S, "mcp_init", lambda **_k: pytest.fail("must not open a session"))
    monkeypatch.setattr("sys.argv", ["snapshot_tools", "--output", str(tmp_path / "x.json")])

    assert S.main() == 1
    # Only what is actually missing is named — reporting the whole list makes
    # the reader check three variables to find the one that is empty.
    err = capsys.readouterr().err
    assert "SW_ACCESS_KEY required" in err
    assert "SW_BASE_URL" not in err


def test_the_store_endpoint_asks_for_the_sales_channel_key(monkeypatch, tmp_path, capsys):
    """The admin pair is meaningless on the Store endpoint, and demanding it
    would block a correctly configured Store snapshot."""
    monkeypatch.setattr(S, "SW_SC_ACCESS_KEY", "")
    monkeypatch.setattr(S, "mcp_init", lambda **_k: pytest.fail("must not open a session"))
    monkeypatch.setattr("sys.argv", ["snapshot_tools", "--endpoint", "store", "--output", str(tmp_path / "x.json")])

    assert S.main() == 1
    assert "SW_SC_ACCESS_KEY required" in capsys.readouterr().err


def test_snapshot_records_the_default_surface_and_the_full_catalogue(monkeypatch, tmp_path, capsys):
    wire(
        monkeypatch,
        default_tools=[{"name": "shopware-tool-search"}],
        full=[
            {"name": "shopware-entity-read", "description": "Read one.", "inputSchema": {"type": "object"}},
            {"name": "shopware-tool-search", "description": "Search.", "inputSchema": {}},
        ],
        toolsets=[{"name": "entity", "title": "Entity tools", "description": "d", "tools": ["shopware-entity-read"]}],
    )

    code, out = run(monkeypatch, tmp_path)
    snap = json.loads(out.read_text())

    assert code == 0
    assert snap["default_tools"] == ["shopware-tool-search"]
    assert snap["server_instructions"] == "Server says hi"
    assert [t["name"] for t in snap["tools"]] == ["shopware-entity-read", "shopware-tool-search"]
    assert "Wrote" in capsys.readouterr().out


def test_the_full_catalogue_is_read_only_after_enabling_every_toolset(monkeypatch, tmp_path):
    """Reading it before enabling would snapshot the three meta-tools as the
    whole catalogue and report every deferred tool as removed."""
    calls = wire(monkeypatch, default_tools=[{"name": "meta"}], full=[{"name": "meta"}, {"name": "deferred"}])

    _, out = run(monkeypatch, tmp_path)

    assert calls["enabled"] == 1
    assert [t["name"] for t in json.loads(out.read_text())["tools"]] == ["deferred", "meta"]


def test_everything_is_sorted_so_a_diff_shows_only_real_churn(monkeypatch, tmp_path):
    wire(
        monkeypatch,
        default_tools=[{"name": "z"}, {"name": "a"}],
        full=[{"name": "z"}, {"name": "a"}],
        toolsets=[
            {"name": "zeta", "tools": ["z-tool", "a-tool"]},
            {"name": "alpha", "tools": ["b-tool"]},
        ],
    )

    _, out = run(monkeypatch, tmp_path)
    snap = json.loads(out.read_text())

    assert snap["default_tools"] == ["a", "z"]
    assert [t["name"] for t in snap["tools"]] == ["a", "z"]
    assert [ts["name"] for ts in snap["toolsets"]] == ["alpha", "zeta"]
    assert snap["toolsets"][1]["tools"] == ["a-tool", "z-tool"]


def test_session_enablement_state_is_dropped_from_the_snapshot(monkeypatch, tmp_path):
    """`enabled` is per-session, so keeping it would make every snapshot differ
    from the last for reasons that are not catalogue changes."""
    wire(monkeypatch, toolsets=[{"name": "entity", "tools": [], "enabled": True}])

    _, out = run(monkeypatch, tmp_path)

    assert "enabled" not in json.loads(out.read_text())["toolsets"][0]


def test_absent_fields_normalise_to_empty_rather_than_missing(monkeypatch, tmp_path):
    """App tools registered from a manifest carry no description; the key must
    still be present or the diff reads as a removed field."""
    wire(monkeypatch, full=[{"name": "bare"}], toolsets=[{"name": "g"}])

    _, out = run(monkeypatch, tmp_path)
    snap = json.loads(out.read_text())

    assert snap["tools"][0] == {"name": "bare", "description": "", "inputSchema": {}}
    assert snap["toolsets"][0] == {"name": "g", "title": "", "description": "", "tools": []}


def test_the_output_directory_is_created(monkeypatch, tmp_path):
    """CI writes into a tool-history/ that does not exist yet on a fresh runner."""
    wire(monkeypatch)
    out = tmp_path / "deep" / "nested" / "latest.json"
    monkeypatch.setattr("sys.argv", ["snapshot_tools", "--output", str(out)])

    assert S.main() == 0
    assert out.exists()


def test_the_file_ends_with_a_newline(monkeypatch, tmp_path):
    """It is committed, so a missing trailing newline shows up in every diff."""
    wire(monkeypatch)

    _, out = run(monkeypatch, tmp_path)

    assert out.read_text().endswith("}\n")
