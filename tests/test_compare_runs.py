"""Unit tests for the cross-model report comparison."""

import json
import sys

import pytest

from eval import compare_runs as C


def report(model, results, errors=()):
    """results: list of (id, passed, expected_tool, skipped); errors: list of ids that errored"""
    errors = set(errors)
    records = []
    for i, p, t, s in results:
        rec = {"id": i, "passed": p, "expected_tool": t, "skipped": s}
        if i in errors:
            rec["error"] = "500 Server Error: Internal Server Error"
        records.append(rec)
    return {"model": model, "modes": {"discovery": {"results": records}}}


def test_splits_fixtures_four_ways():
    a = report(
        "strong",
        [("f1", True, "t1", False), ("f2", True, "t1", False), ("f3", False, "t2", False), ("f4", False, "t2", False)],
    )
    b = report(
        "weak",
        [("f1", True, "t1", False), ("f2", False, "t1", False), ("f3", True, "t2", False), ("f4", False, "t2", False)],
    )

    c = C.compare(a, b)

    assert c["both_pass"] == ["f1"]
    assert c["only_primary"] == ["f2"]
    assert c["only_second"] == ["f3"]
    assert c["both_fail"] == ["f4"]


def test_skipped_fixtures_are_excluded_from_rates():
    """Skipped fixtures never gate, so they must not dilute either rate."""
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", True)])
    b = report("weak", [("f1", True, "t1", False), ("f2", False, "t1", True)])

    c = C.compare(a, b)

    assert c["primary"]["total"] == 1
    assert c["primary"]["rate"] == 1.0
    assert c["shared"] == 1


def test_errored_fixtures_are_not_counted_as_failures():
    """The regression this guards: 18 server 500s once read as a 53% model score.

    Every fixture but one errors; the model got one right out of one that ran,
    so the rate is 100% with 3 errored — not 25%.
    """
    a = report(
        "strong",
        [("f1", True, "t1", False), ("f2", False, "t1", False), ("f3", False, "t1", False), ("f4", False, "t1", False)],
        errors=["f2", "f3", "f4"],
    )

    c = C.compare(a, a)

    assert c["primary"]["passed"] == 1
    assert c["primary"]["total"] == 1
    assert c["primary"]["rate"] == 1.0
    assert c["primary"]["errored"] == 3
    assert c["both_fail"] == []


def test_errored_fixtures_are_excluded_from_the_shared_comparison():
    """A fixture that errored for one model cannot be attributed to either."""
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", False)], errors=["f2"])
    b = report("weak", [("f1", True, "t1", False), ("f2", False, "t1", False)])

    c = C.compare(a, b)

    assert c["shared"] == 1
    assert c["both_fail"] == []
    assert c["only_primary"] == []
    # f2 errored, not mismatched — the fixture files still agree.
    assert c["unmatched"] == []


def test_render_warns_when_fixtures_errored():
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", False)], errors=["f2"])
    out = C.render(C.compare(a, a), 0.9)

    assert "never reached the model" in out
    assert "Errored" in out


def test_both_fail_is_grouped_by_tool():
    a = report("strong", [("f1", False, "t1", False), ("f2", False, "t1", False), ("f3", False, "t2", False)])
    b = report("weak", [("f1", False, "t1", False), ("f2", False, "t1", False), ("f3", False, "t2", False)])

    c = C.compare(a, b)

    assert c["both_fail_by_tool"] == {"t1": ["f1", "f2"], "t2": ["f3"]}


def test_unmatched_fixtures_are_flagged():
    """Different fixture sets between runs make the comparison meaningless."""
    a = report("strong", [("f1", True, "t1", False), ("only_in_a", True, "t1", False)])
    b = report("weak", [("f1", True, "t1", False), ("only_in_b", True, "t1", False)])

    c = C.compare(a, b)

    assert c["unmatched"] == ["only_in_a", "only_in_b"]
    assert c["shared"] == 1


def test_missing_discovery_mode_yields_empty_index():
    assert C.discovery_index({"modes": {"baseline": {"results": [{"id": "f1", "passed": True}]}}}) == {}
    assert C.pass_rate({}) == (0, 0, 0.0)


def test_render_lists_worst_tool_first():
    a = report("strong", [("f1", False, "lonely", False), ("f2", False, "busy", False), ("f3", False, "busy", False)])
    out = C.render(C.compare(a, a), 0.9)

    assert out.index("`busy`") < out.index("`lonely`")
    assert "3" in out


@pytest.mark.parametrize(
    "gate,strong_pass,weak_pass,expected",
    [
        ("none", False, False, 0),  # report only, never gates
        ("primary", True, False, 0),  # weak model below threshold is tolerated
        ("primary", False, True, 1),
        ("both", True, True, 0),
        ("both", True, False, 1),  # this is the "both over 90%" mode
    ],
)
def test_gate_modes(tmp_path, monkeypatch, capsys, gate, strong_pass, weak_pass, expected):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report("strong", [("f1", strong_pass, "t1", False)])))
    b.write_text(json.dumps(report("weak", [("f1", weak_pass, "t1", False)])))

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(a), str(b), "--gate", gate])

    assert C.main() == expected
    capsys.readouterr()


def test_step_summary_is_left_to_the_summary_renderer(tmp_path, monkeypatch, capsys):
    """This script must not touch GITHUB_STEP_SUMMARY any more.

    eval/summary.py renders the job summary once, from --output. Appending here
    too would put a second copy of these tables in the middle of it.
    """
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report("strong", [("f1", True, "t1", False)])))
    b.write_text(json.dumps(report("weak", [("f1", True, "t1", False)])))
    summary = tmp_path / "summary.md"
    out_json = tmp_path / "cmp.json"

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(a), str(b), "--output", str(out_json)])

    assert C.main() == 0
    assert not summary.exists()
    assert json.loads(out_json.read_text())["shared"] == 1
    assert "Cross-model comparison" in capsys.readouterr().out


def test_both_fail_detail_records_what_each_model_picked():
    """The confusion pair is the finding — naming only the expected tool says a
    description is wrong but not what it lost to."""
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": None, "fail_reason": "no_tool_call"}

    detail = C.compare(a, b)["both_fail_detail"]

    expected = {
        "id": "f1",
        "expected_tool": "wanted",
        "primary_selected": "sibling",
        "second_selected": None,
        "primary_reason": "wrong_tool",
        "second_reason": "no_tool_call",
    }

    assert len(detail) == 1
    # Subset, not equality: the record also carries the prompt, descriptions and
    # trail, which the detail-block tests below cover.
    assert {k: detail[0].get(k) for k in expected} == expected


def test_both_fail_detail_carries_the_material_needed_to_rewrite_the_description():
    """The confusion pair names which two descriptions overlap; it does not show
    them. Fetching them by hand from the run artifact is the step that stopped
    anyone acting on these rows, so the prompt, both descriptions and the
    discovery trail travel with the finding."""
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {
        "selected_tool": "sibling",
        "fail_reason": "wrong_tool",
        "prompt": "Read me the entity-definition skill.",
        "expected_toolset": "dev-skills",
        "meta_calls": [
            {"tool": "shopware-toolsets-list", "input": {}},
            {"tool": "shopware-toolset-enable", "input": {"toolset": "dev-skills"}},
        ],
    }
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}

    d = C.compare(a, b, {"wanted": "Reads one skill body.", "sibling": "Lists the skills."})["both_fail_detail"][0]

    assert d["prompt"] == "Read me the entity-definition skill."
    assert d["expected_toolset"] == "dev-skills"
    assert d["descriptions"] == {"wanted": "Reads one skill body.", "sibling": "Lists the skills."}
    assert d["primary_trail"] == "shopware-toolsets-list → shopware-toolset-enable(dev-skills)"
    # No meta calls on the second run: it never tried to discover anything, which
    # is a different failure from picking the wrong tool after discovering well.
    assert d["second_trail"] == "went straight to a tool — no discovery calls"


def test_detail_omits_descriptions_when_no_catalogue_is_available():
    """The snapshot is optional — a comparison run without it still renders."""
    a = report("strong", [("f1", False, "wanted", False)])

    assert C.compare(a, a)["both_fail_detail"][0]["descriptions"] == {}


def test_both_fail_detail_degrades_when_fields_are_absent():
    """Older reports have no selected_tool/fail_reason; that must not raise."""
    a = report("strong", [("f1", False, "t1", False)])

    detail = C.compare(a, a)["both_fail_detail"]

    assert detail[0]["primary_selected"] is None
    assert detail[0]["primary_reason"] is None


def test_actionable_table_shows_both_picks_and_hides_wrong_tool():
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling_a", "fail_reason": "wrong_tool"}
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling_b", "fail_reason": "wrong_tool"}

    out = C.render_actionable(C.compare(a, b), "strong", "weak")

    assert "`sibling_a`" in out
    assert "`sibling_b`" in out
    # Redundant with the two picked columns, so it is not printed.
    assert "wrong_tool" not in out


def test_detail_block_renders_the_prompt_both_descriptions_and_the_trail():
    """The table names the confusion pair; this block is what you rewrite
    against, so it must carry the prompt and both descriptions in full."""
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {
        "selected_tool": "sibling",
        "fail_reason": "wrong_tool",
        "prompt": "Read me the entity-definition skill.",
        "category": "unambiguous",
        "meta_calls": [{"tool": "shopware-toolset-enable", "input": {"toolset": "dev-skills"}}],
    }
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}
    catalogue = {"wanted": "Reads one skill body by name.", "sibling": "Lists every available skill."}

    out = C.render_detail(C.compare(a, b, catalogue), "strong", "weak")

    assert "Read me the entity-definition skill." in out
    assert "Reads one skill body by name." in out
    assert "Lists every available skill." in out
    assert "(expected)" in out and "(picked instead)" in out
    assert "shopware-toolset-enable(dev-skills)" in out
    # Collapsed, so a handful of full descriptions cannot bury the tables above.
    assert out.startswith("<details>") and "</details>" in out


def test_detail_block_does_not_repeat_a_description_both_models_picked():
    """Both models usually reach for the same wrong tool; printing its
    description twice doubles the block for no gain."""
    a = report("strong", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}

    out = C.render_detail(C.compare(a, a, {"wanted": "W.", "sibling": "Overlapping text."}))

    assert out.count("Overlapping text.") == 1


def test_detail_block_is_empty_when_nothing_both_failed():
    a = report("strong", [("f1", True, "wanted", False)])

    assert C.render_detail(C.compare(a, a)) == ""


def test_detail_block_says_so_when_a_description_is_missing_from_the_snapshot():
    """A tool added since the snapshot, or a snapshot that never got written —
    better to name the gap than to render a blank quote."""
    a = report("strong", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}

    out = C.render_detail(C.compare(a, a, {}))

    assert "description not in the catalogue snapshot" in out


def test_actionable_table_keeps_reasons_the_columns_cannot_show():
    a = report("strong", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": None, "fail_reason": "step_cap"}

    out = C.render_actionable(C.compare(a, a))

    assert "both: step_cap" in out
    assert "(none)" in out


def test_actionable_table_says_so_when_nothing_failed_twice():
    a = report("strong", [("f1", True, "t1", False)])
    assert "No fixture failed for both models." in C.render_actionable(C.compare(a, a))


def test_actionable_table_lists_one_row_per_fixture():
    a = report("strong", [("f1", False, "busy", False), ("f2", False, "busy", False), ("f3", False, "lonely", False)])

    out = C.render_actionable(C.compare(a, a))
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "|---" not in ln and "Expected tool" not in ln]

    assert len(rows) == 3
    assert out.index("`busy`") < out.index("`lonely`")


def test_actionable_table_puts_core_failures_above_plugin_failures():
    """Owner decides urgency: a core description problem outranks a plugin one
    however many prompts the plugin lost."""
    a = report(
        "strong",
        [
            ("p1", False, "merchant-order-summary", False),
            ("p2", False, "merchant-order-summary", False),
            ("c1", False, "shopware-entity-read", False),
        ],
    )

    out = C.render_actionable(C.compare(a, a))

    assert out.index("shopware-entity-read") < out.index("merchant-order-summary")


def test_actionable_table_names_the_owning_repository():
    a = report("strong", [("f1", False, "swag-dev-tools-load-skill", False)])

    out = C.render_actionable(C.compare(a, a))

    assert "| dev-tools |" in out


def test_unreadable_report_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")])
    assert C.main() == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Catalogue loading and the remaining render edges
# ---------------------------------------------------------------------------
def test_catalogue_maps_tool_name_to_description(tmp_path):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"tools": [{"name": "a", "description": "A does things."}]}))

    assert C.load_catalogue(str(snap)) == {"a": "A does things."}


def test_catalogue_normalises_a_null_description_to_empty():
    """App tools from a manifest carry none; None would render as the word None."""
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "s.json"
        p.write_text(json.dumps({"tools": [{"name": "a", "description": None}, {"no_name": 1}]}))

        assert C.load_catalogue(str(p)) == {"a": ""}


def test_catalogue_is_optional(capsys):
    assert C.load_catalogue(None) == {}


def test_an_unreadable_catalogue_warns_but_does_not_fail_the_comparison(capsys, tmp_path):
    """The snapshot is a convenience; losing it must not lose the findings."""
    assert C.load_catalogue(str(tmp_path / "absent.json")) == {}
    assert "::warning::Could not read tool catalogue" in capsys.readouterr().err


def test_note_distinguishes_the_two_models_when_only_one_stalled():
    assert C._note("step_cap", "wrong_tool") == "primary: step_cap"
    assert C._note("wrong_tool", "no_tool_call") == "second: no_tool_call"
    assert C._note("step_cap", "no_tool_call") == "primary: step_cap, second: no_tool_call"


def test_detail_block_reports_the_category_and_toolset_when_known():
    a = report("strong", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {
        "selected_tool": "sibling",
        "fail_reason": "wrong_tool",
        "category": "disambiguation",
        "expected_toolset": "dev-skills",
        "notes": "the index vs one body",
    }

    out = C.render_detail(C.compare(a, a, {"wanted": "W", "sibling": "S"}))

    assert "category: disambiguation" in out
    assert "toolset: `dev-skills`" in out
    assert "Fixture note: the index vs one body" in out


def test_unmatched_fixtures_warn_that_the_runs_are_not_comparable():
    a = report("strong", [("f1", True, "t", False)])
    b = report("weak", [("f2", True, "t", False)])

    out = C.render_unmatched(C.compare(a, b))

    assert "2 fixture(s) graded in only one run" in out
    assert "not comparable" in out


def test_no_warning_when_both_runs_graded_the_same_set():
    a = report("strong", [("f1", True, "t", False)])

    assert C.render_unmatched(C.compare(a, a)) == ""
