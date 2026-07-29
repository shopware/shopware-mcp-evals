"""Naming what moved between two catalogue snapshots.

The ordering in the rendered output is the point: a tool leaving the default
surface or a toolset being resliced changes what every fixture has to discover,
while a description edit affects one. A reader skimming a nightly at 6am should
hit the structural findings first.
"""

import json

from eval import drift as D


def snap(tools=None, default_tools=None, toolsets=None, instructions="hello"):
    return {
        "server_instructions": instructions,
        "default_tools": default_tools if default_tools is not None else ["shopware-tool-search"],
        "toolsets": toolsets if toolsets is not None else [{"name": "entity", "tools": ["shopware-entity-read"]}],
        "tools": tools
        if tools is not None
        else [{"name": "shopware-entity-read", "description": "d", "inputSchema": {}}],
    }


def tool(name, description="d", schema=None):
    return {"name": name, "description": description, "inputSchema": schema if schema is not None else {}}


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------
def test_identical_snapshots_are_not_drift():
    s = D.summarise(snap(), snap())

    assert D.is_significant(s) is False
    assert "No catalogue drift" in D.render(s)


def test_a_changed_description_is_named():
    s = D.summarise(snap([tool("a", "before")]), snap([tool("a", "after")]))

    assert s["described"] == ["a"]
    assert D.is_significant(s)


def test_added_and_removed_tools_are_separated():
    s = D.summarise(snap([tool("a"), tool("gone")]), snap([tool("a"), tool("new")]))

    assert s["added"] == ["new"]
    assert s["removed"] == ["gone"]


def test_a_schema_change_is_reported_apart_from_a_description_change():
    """They need different responses: a schema change can break every call to the
    tool, a description change only changes which tool gets picked."""
    s = D.summarise(snap([tool("a", "d", {})]), snap([tool("a", "d", {"type": "object"})]))

    assert s["schema"] == ["a"] and s["described"] == []


def test_default_surface_change_is_flagged():
    s = D.summarise(snap(default_tools=["a"]), snap(default_tools=["a", "b"]))

    assert s["default_surface"] is True


def test_server_instructions_change_is_flagged():
    """They are part of the system prompt every run sends, so a change there moves
    the whole eval, not one fixture."""
    s = D.summarise(snap(instructions="old"), snap(instructions="new"))

    assert s["instructions"] is True


# ---------------------------------------------------------------------------
# Toolset-level changes
# ---------------------------------------------------------------------------
def test_a_resliced_toolset_is_reported_as_membership():
    old = snap(toolsets=[{"name": "entity", "tools": ["a", "b"]}])
    new = snap(toolsets=[{"name": "entity", "tools": ["a"]}])

    assert D.summarise(old, new)["toolsets"]["membership"] == ["entity"]


def test_added_and_removed_toolsets_are_separated():
    old = snap(toolsets=[{"name": "entity", "tools": []}, {"name": "gone", "tools": []}])
    new = snap(toolsets=[{"name": "entity", "tools": []}, {"name": "fresh", "tools": []}])
    ts = D.summarise(old, new)["toolsets"]

    assert ts["added"] == ["fresh"] and ts["removed"] == ["gone"]


def test_membership_tolerates_both_wire_shapes():
    """Bare names today; {name, title} pairs if shopware/shopware#18762 returns.
    A shape change alone must not read as every group being resliced."""
    old = snap(toolsets=[{"name": "entity", "tools": ["a", "b"]}])
    new = snap(toolsets=[{"name": "entity", "tools": [{"name": "b", "title": "B"}, {"name": "a", "title": "A"}]}])

    assert D.summarise(old, new)["toolsets"]["membership"] == []


def test_reordering_alone_is_not_drift():
    old = snap(toolsets=[{"name": "entity", "tools": ["b", "a"]}])
    new = snap(toolsets=[{"name": "entity", "tools": ["a", "b"]}])

    assert D.summarise(old, new)["toolsets"]["membership"] == []


# ---------------------------------------------------------------------------
# render — ordering and content
# ---------------------------------------------------------------------------
def test_structural_findings_come_before_description_churn():
    old = snap([tool("a", "before")], default_tools=["x"])
    new = snap([tool("a", "after")], default_tools=["x", "y"])

    out = D.render(D.summarise(old, new))

    assert out.index("default advertised surface") < out.index("Descriptions changed")


def test_added_tools_are_flagged_as_needing_fixtures():
    out = D.render(D.summarise(snap([tool("a")]), snap([tool("a"), tool("b")])))

    assert "`b`" in out and "need fixtures" in out


def test_removed_tools_warn_that_their_fixtures_will_skip():
    out = D.render(D.summarise(snap([tool("a"), tool("b")]), snap([tool("a")])))

    assert "`b`" in out and "will skip" in out


def test_long_lists_are_truncated_with_a_count():
    many = [tool(f"t{i}") for i in range(20)]
    out = D.render(D.summarise(snap(many), snap([tool(f"t{i}", "changed") for i in range(20)])))

    assert "+8 more" in out


def test_render_always_says_how_to_reconcile():
    out = D.render(D.summarise(snap([tool("a", "x")]), snap([tool("a", "y")])))

    assert "shopware.sha" in out and "tool-history/latest.json" in out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run(monkeypatch, old, new, *extra):
    monkeypatch.setattr("sys.argv", ["eval.drift", "--old", str(old), "--new", str(new), *extra])
    return D.main()


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_cli_exits_zero_and_says_nothing_changed(tmp_path, monkeypatch, capsys):
    a = write(tmp_path, "a.json", snap())

    assert run(monkeypatch, a, a) == 0
    assert "No catalogue drift" in capsys.readouterr().out


def test_cli_exit_code_flag_signals_drift_for_shell_branching(tmp_path, monkeypatch, capsys):
    a = write(tmp_path, "a.json", snap([tool("a", "before")]))
    b = write(tmp_path, "b.json", snap([tool("a", "after")]))

    assert run(monkeypatch, a, b, "--exit-code") == 1
    assert run(monkeypatch, a, b) == 0, "without the flag it only reports"
    capsys.readouterr()


def test_a_missing_baseline_is_drift_but_never_crashes_the_step(tmp_path, monkeypatch, capsys):
    """The first run on a fresh repo has no baseline, and a workflow step that
    raised there would fail the whole job over a reporting convenience."""
    b = write(tmp_path, "b.json", snap())

    assert run(monkeypatch, tmp_path / "absent.json", b, "--exit-code") == 1
    out = capsys.readouterr()
    assert "treating as drift" in out.out
    assert "::warning::" in out.err


def test_the_heading_can_be_set_for_the_job_summary(tmp_path, monkeypatch, capsys):
    a = write(tmp_path, "a.json", snap([tool("a", "x")]))
    b = write(tmp_path, "b.json", snap([tool("a", "y")]))

    run(monkeypatch, a, b, "--heading", "Nightly drift vs trunk")

    assert "## Nightly drift vs trunk" in capsys.readouterr().out
