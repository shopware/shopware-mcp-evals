"""Unit tests for the consolidated job-summary renderer."""

import json
import sys

from eval import summary as S


def row(suite, model, rate, graded, gate="PASS", advisory=False, **extra):
    return {
        "suite": suite,
        "provider": "openai",
        "model": model,
        "rate": rate,
        "graded": graded,
        "errored": 0,
        "throttled": 0,
        "gate": gate,
        "advisory": advisory,
        **extra,
    }


def comparison(both_fail_detail=(), **extra):
    ids = [d["id"] for d in both_fail_detail]
    by_tool = {}
    for d in both_fail_detail:
        by_tool.setdefault(d["expected_tool"], []).append(d["id"])
    return {
        "primary": {"model": "gpt-4o", "passed": 83, "total": 90, "rate": 0.92, "errored": 0},
        "second": {"model": "gpt-4o-mini", "passed": 82, "total": 90, "rate": 0.91, "errored": 0},
        "shared": 90,
        "both_pass": ["ok"] * 77,
        "only_primary": ["p"] * 6,
        "only_second": ["s"] * 5,
        "both_fail": ids,
        "both_fail_by_tool": by_tool,
        "both_fail_detail": list(both_fail_detail),
        "unmatched": [],
        **extra,
    }


def write_rows(tmp_path, rows):
    d = tmp_path / "rows"
    d.mkdir()
    for i, r in enumerate(rows, start=1):
        (d / f"{i}-{r['suite'].split()[0]}.json").write_text(json.dumps(r))
    return d


def test_all_runs_land_in_one_table_in_filename_order(tmp_path):
    rows = S.load_rows(
        write_rows(
            tmp_path,
            [
                row("admin · primary", "gpt-4o", 0.92, 90),
                row("admin · second validator", "gpt-4o-mini", 0.91, 90),
                row("store · UCP", "gpt-4o", 0.90, 42, advisory=True),
            ],
        )
    )
    out = S.render_runs(rows)

    # One header, not one per run — the whole point of collecting rows.
    assert out.count("| Suite | Provider | Model |") == 1
    assert out.index("admin · primary") < out.index("admin · second validator") < out.index("store · UCP")
    assert "92%" in out and "91%" in out and "90%" in out


def test_advisory_run_is_marked_so_its_verdict_is_not_read_as_the_gate(tmp_path):
    rows = S.load_rows(write_rows(tmp_path, [row("store · UCP", "gpt-4o", 0.5, 42, gate="FAIL", advisory=True)]))
    out = S.render_runs(rows)

    assert "FAIL (advisory)" in out


def test_gating_run_is_not_marked_advisory(tmp_path):
    rows = S.load_rows(write_rows(tmp_path, [row("admin · primary", "gpt-4o", 0.5, 90, gate="FAIL")]))
    assert "advisory" not in S.render_runs(rows)


def test_missing_store_row_just_leaves_two_rows(tmp_path):
    rows = S.load_rows(
        write_rows(
            tmp_path,
            [row("admin · primary", "gpt-4o", 0.92, 90), row("admin · second validator", "gpt-4o-mini", 0.91, 90)],
        )
    )
    out = S.render_runs(rows)

    assert len([ln for ln in out.splitlines() if ln.startswith("| admin")]) == 2
    assert "store" not in out


def test_no_rows_at_all_is_stated_rather_than_rendered_as_an_empty_table(tmp_path):
    assert "No eval run reported a result." in S.render_runs(S.load_rows(tmp_path / "absent"))


def test_unreadable_row_is_skipped_not_fatal(tmp_path):
    d = write_rows(tmp_path, [row("admin · primary", "gpt-4o", 0.92, 90)])
    (d / "9-broken.json").write_text("{not json")

    rows = S.load_rows(d)

    assert len(rows) == 1


def tier(passed, total, failed_ids=(), ids=None):
    """A by_tier bucket. `ids` defaults to None to exercise the legacy path —
    rows written before breakdown() emitted the full id list."""
    bucket = {"passed": passed, "total": total, "failed_ids": list(failed_ids), "rate": passed / total}
    if ids is not None:
        bucket["ids"] = list(ids)
    return bucket


def test_tier_table_sums_the_same_owner_across_suites():
    rows = [
        row("admin · primary", "gpt-4o", 0.9, 90, by_tier={"core": tier(40, 42), "dev-tools": tier(20, 21, ["d1"])}),
        row("store · UCP", "gpt-4o", 0.9, 42, advisory=True, by_tier={"core": tier(2, 3, ["s1"])}),
    ]

    out = S.render_tiers(rows)

    # 42 admin core fixtures + 3 store core fixtures, one table line.
    assert "| core | 42/45 |" in out
    assert "| dev-tools | 20/21 |" in out


def test_a_fixture_graded_by_two_suites_counts_once():
    """The admin primary and the second validator grade the same fixture set.
    Summing `total` reported dev-tools out of 42 when there are 21 dev-tools
    fixtures, and listed a both-model failure twice in the failing cell."""
    dev = tier(1, 3, ["d1", "d2"], ids=["d1", "d2", "d3"])
    rows = [
        row("admin · primary", "gpt-5.4-mini", 0.9, 3, by_tier={"dev-tools": dev}),
        row("admin · second", "gpt-4o-mini", 0.9, 3, by_tier={"dev-tools": dev}),
    ]

    out = S.render_tiers(rows)

    assert "| dev-tools | 1/3 |" in out, "denominator must be 3 fixtures, not 6 fixture-runs"
    assert out.count("`d1`") == 1, "a fixture failing on both models is still one fixture"


def test_a_fixture_failing_on_every_model_is_marked_as_the_actionable_one():
    """This is the column that reconciles By-owner with the cross-model table:
    without it, a reader works through capability-gap misses that no description
    change can fix."""
    rows = [
        # d1 fails in both runs; d2 only in the first.
        row("a", "strong", 0.5, 2, by_tier={"dev-tools": tier(0, 2, ["d1", "d2"], ids=["d1", "d2"])}),
        row("b", "weak", 0.5, 2, by_tier={"dev-tools": tier(1, 2, ["d1"], ids=["d1", "d2"])}),
    ]

    out = S.render_tiers(rows)

    assert "**`d1`**" in out
    assert "`d2`" in out and "**`d2`**" not in out
    # One of the two failures failed everywhere.
    assert "| 0/2 | 0% | 1 |" in out


def test_a_fixture_only_one_model_graded_is_not_called_a_confirmed_failure():
    """The Store suite runs a single model. Without a two-run floor its failures
    satisfied "failed on every model that graded it" and were bolded as
    actionable — the By-owner table reported two while the cross-model table
    right below it reported one, which is the single-run noise the two-model
    split exists to discount."""
    rows = [
        row("admin · primary", "strong", 0.9, 2, by_tier={"core": tier(1, 2, ["a1"], ids=["a1", "a2"])}),
        row("admin · second", "weak", 0.9, 2, by_tier={"core": tier(1, 2, ["a1"], ids=["a1", "a2"])}),
        # One model, one fixture, one failure.
        row(
            "store · UCP", "strong", 0.9, 1, advisory=True, by_tier={"agentic-commerce": tier(0, 1, ["s1"], ids=["s1"])}
        ),
    ]

    out = S.render_tiers(rows)

    assert "**`a1`**" in out, "graded twice and failed twice is a confirmed finding"
    assert "`s1`" in out and "**`s1`**" not in out, "graded once is not evidence about a description"
    assert "| agentic-commerce | 0/1 | 0% | — |" in out, "and it must not be counted in the column"


def test_a_fixture_that_two_models_graded_and_one_passed_is_not_bolded():
    rows = [
        row("a", "strong", 0.5, 1, by_tier={"core": tier(1, 1, [], ids=["x"])}),
        row("b", "weak", 0.5, 1, by_tier={"core": tier(0, 1, ["x"], ids=["x"])}),
    ]

    out = S.render_tiers(rows)

    assert "`x`" in out and "**`x`**" not in out


def test_bolded_failures_survive_truncation():
    """With a cap of six, an owner with many capability-gap misses could
    otherwise hide its only actionable fixture."""
    many = [f"z{i}" for i in range(8)]
    rows = [
        row("a", "strong", 0.1, 9, by_tier={"core": tier(0, 9, [*many, "actionable"], ids=[*many, "actionable"])}),
        row("b", "weak", 0.1, 9, by_tier={"core": tier(8, 9, ["actionable"], ids=[*many, "actionable"])}),
    ]

    out = S.render_tiers(rows)

    assert "**`actionable`**" in out
    assert "more)" in out, "the rest should still be summarised as a count"


def test_tier_table_says_core_gates_and_plugins_only_count_towards_the_suite():
    rows = [row("admin · primary", "gpt-4o", 0.9, 90, by_tier={"core": tier(40, 42), "merchant-tools": tier(27, 27)})]

    out = S.render_tiers(rows)

    assert "**core gate**" in out
    assert "| merchant-tools | 27/27 | 100% | — | suite rate | — |" in out


def test_tier_from_an_advisory_suite_only_is_marked_advisory():
    """The whole store step is continue-on-error, so its UCP numbers enforce
    nothing — saying 'suite rate' there would overstate them."""
    rows = [row("store · UCP", "gpt-4o", 0.9, 42, advisory=True, by_tier={"agentic-commerce": tier(35, 39)})]

    out = S.render_tiers(rows)

    assert "| agentic-commerce | 35/39 | 90% |" in out
    assert "advisory" in out
    # This row carries no `ids`, so the four failures have no fixture name to
    # print. They must still count against the denominator.
    assert "4 unnamed (older run)" in out


def test_core_stays_gating_even_when_an_advisory_suite_also_contributes_to_it():
    rows = [
        row("admin · primary", "gpt-4o", 0.9, 90, by_tier={"core": tier(40, 42)}),
        row("store · UCP", "gpt-4o", 0.9, 42, advisory=True, by_tier={"core": tier(3, 3)}),
    ]

    assert "**core gate**" in S.render_tiers(rows)


def test_tier_table_names_the_failing_fixtures_and_caps_the_list():
    ids = [f"f{i}" for i in range(9)]
    rows = [row("admin · primary", "gpt-4o", 0.9, 90, by_tier={"core": tier(33, 42, ids)})]

    out = S.render_tiers(rows)

    assert "`f0`" in out and "`f5`" in out
    assert "(+3 more)" in out
    assert "`f8`" not in out


def test_tier_table_orders_discovery_and_core_before_the_plugins():
    rows = [
        row(
            "admin · primary",
            "gpt-4o",
            0.9,
            90,
            by_tier={"merchant-tools": tier(27, 27), "core · discovery": tier(9, 9), "core": tier(33, 33)},
        )
    ]

    out = S.render_tiers(rows)

    assert out.index("core · discovery") < out.index("| core |") < out.index("merchant-tools")


def test_tier_table_is_absent_when_no_row_carries_a_breakdown():
    assert S.render_tiers([row("admin · primary", "gpt-4o", 0.92, 90)]) == ""


def test_comparison_section_omits_the_rates_already_in_the_run_table():
    out = S.render_comparison(comparison())

    assert "| Outcome | Count | Meaning |" in out
    # The per-model rate table belongs to the run table above; repeating it here
    # is the redundancy this renderer exists to remove.
    assert "| Model | Passed | Rate |" not in out
    assert "vs threshold" not in out


def test_comparison_section_names_what_each_model_picked():
    out = S.render_comparison(
        comparison(
            both_fail_detail=[
                {
                    "id": "list_skills_available",
                    "expected_tool": "swag-dev-tools-list-skills",
                    "primary_selected": "swag-dev-tools-load-skill",
                    "second_selected": "shopware-tool-search",
                    "primary_reason": "wrong_tool",
                    "second_reason": "wrong_tool",
                }
            ]
        )
    )

    assert "`swag-dev-tools-list-skills`" in out
    assert "`list_skills_available`" in out
    assert "`swag-dev-tools-load-skill`" in out
    assert "`shopware-tool-search`" in out
    # Column headers carry role + model, so the picks stay attributable even
    # when both runs used the same model.
    assert "primary `gpt-4o` picked" in out
    assert "second `gpt-4o-mini` picked" in out


def test_missing_comparison_is_explained_not_silently_dropped():
    out = S.render_comparison(None)

    assert "Not available" in out
    assert "did not finish" in out


def test_main_appends_once_to_the_step_summary(tmp_path, monkeypatch, capsys):
    rows_dir = write_rows(tmp_path, [row("admin · primary", "gpt-4o", 0.92, 90)])
    cmp_path = tmp_path / "cmp.json"
    cmp_path.write_text(json.dumps(comparison()))
    summary = tmp_path / "summary.md"

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["summary.py", "--rows", str(rows_dir), "--comparison", str(cmp_path)])

    assert S.main() == 0
    text = summary.read_text()
    assert text.count("## MCP evals") == 1
    assert text.count("| Suite | Provider | Model |") == 1
    capsys.readouterr()


def test_main_survives_a_missing_comparison_file(tmp_path, monkeypatch, capsys):
    rows_dir = write_rows(tmp_path, [row("admin · primary", "gpt-4o", 0.92, 90)])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["summary.py", "--rows", str(rows_dir), "--comparison", str(tmp_path / "nope")])

    assert S.main() == 0
    assert "Not available" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Per-tool scorecard section
# ---------------------------------------------------------------------------


def fixture_result(fid, expected, selected, passed):
    return {"id": fid, "expected_tool": expected, "selected_tool": selected, "passed": passed}


def report(*results):
    return {"modes": {"discovery": {"results": list(results)}}}


def tool_rows(text):
    """Tool names from the scorecard table, in rendered order."""
    return [
        line.split("`")[1] for line in text.splitlines() if line.startswith("| `") and "×" not in line.split("|")[1]
    ]


def test_load_reports_warns_about_a_corrupt_report_but_not_a_skipped_one(tmp_path, capsys):
    """The Store suite is conditional, so an absent report is the normal case.

    Warning on it would fire on most runs, and a warning that always fires is
    one nobody reads — including the corrupt-file warning that matters.
    """
    good = tmp_path / "eval-primary.json"
    good.write_text(json.dumps(report(fixture_result("a", "alpha", "alpha", True))))
    broken = tmp_path / "eval-broken.json"
    broken.write_text("{not json")

    loaded = S.load_reports([str(good), str(broken), str(tmp_path / "eval-store.json")])

    assert len(loaded) == 1
    warnings = capsys.readouterr().err
    assert "eval-broken.json" in warnings
    assert "eval-store.json" not in warnings


def test_load_reports_without_paths_is_empty():
    assert S.load_reports(None) == []


def test_pooled_results_keeps_every_observation_across_runs():
    """Pooling, not deduping: precision is a property of the description, so a
    miss under either model is evidence about the same description."""
    primary = report(fixture_result("shared", "alpha", "alpha", True))
    second = report(fixture_result("shared", "alpha", "beta", False))

    assert len(S.pooled_results([primary, second])) == 2


def test_pooled_results_excludes_the_triage_arms():
    """The arms re-run the same failures under a different advertised surface.
    Folding them in counted one failure three times and inflated every
    confusion pair by however many arms happened to run."""
    both = {
        "modes": {
            "discovery": {"results": [fixture_result("f", "alpha", "beta", False)]},
            "isolated": {"results": [fixture_result("f", "alpha", "beta", False)]},
            "full": {"results": [fixture_result("f", "alpha", "beta", False)]},
        }
    }

    pooled = S.pooled_results([both])

    assert len(pooled) == 1


def test_load_catalog_returns_full_definitions_and_survives_a_missing_file(tmp_path, capsys):
    snap = tmp_path / "latest.json"
    snap.write_text(json.dumps({"tools": [{"name": "alpha", "description": "d", "inputSchema": {}}, {"no": "name"}]}))

    catalog = S.load_catalog(str(snap))
    assert list(catalog) == ["alpha"]
    assert catalog["alpha"]["inputSchema"] == {}

    assert S.load_catalog(str(tmp_path / "nope.json")) == {}
    assert "Could not read tool catalogue" in capsys.readouterr().err
    assert S.load_catalog(None) == {}


def test_scorecard_section_is_omitted_when_there_is_nothing_to_score():
    assert S.render_tool_scorecard([], {}) == ""
    assert S.render_tool_scorecard([fixture_result("n", None, None, True)], {}) == ""


def test_scorecard_section_names_the_thief_in_the_victims_row():
    """`beta` wins its own fixture and steals alpha's — recall alone calls beta
    perfect (1/1) and says nothing about the theft.

    Rows are ranked worst-quality-first, so `alpha` leads: it is the tool that
    won nothing. What makes the table actionable is that alpha's row names beta
    as the cause, and the pair is restated below.
    """
    text = S.render_tool_scorecard(
        [
            fixture_result("a1", "alpha", "beta", False),
            fixture_result("b1", "beta", "beta", True),
        ],
        {},
    )
    # Twice each: once in the visible "worth acting on" table, once in the full
    # table collapsed underneath. Both tools qualify here — alpha won nothing
    # (recall 0) and beta is right only half the time it is picked.
    assert tool_rows(text) == ["alpha", "beta", "alpha", "beta"]
    assert "2 of 2 tools below 90%" in text
    assert "| `beta` | 1 | 100% | 2 | 50% |" in text
    assert "`alpha` ×1" in text
    assert "Confusion pairs" in text
    assert "`alpha` / `beta` — 1 miss(es)" in text


def test_a_clean_tool_is_only_in_the_collapsed_table():
    """The point of the split: a tool nobody needs to look at must not take a row
    in front of the reader. It is still in the full table, because comparing two
    runs by hand needs every row."""
    text = S.render_tool_scorecard([fixture_result("a1", "alpha", "alpha", True)], {})

    visible, _, collapsed = text.partition("<details>")
    assert "All 1 tools are at or above 90% on both." in visible
    assert "`alpha`" not in visible
    assert "`alpha`" in collapsed


def test_a_tool_no_fixture_covers_is_flagged_even_though_it_has_no_rates():
    """An uncovered tool is the one failure the rates cannot show — it has no
    recall to be bad."""
    text = S.render_tool_scorecard([fixture_result("a1", "alpha", "alpha", True)], {"ghost": {"name": "ghost"}})
    visible = text.partition("<details>")[0]

    assert "`ghost`" in visible


def test_the_confusion_tail_is_collapsed_so_it_cannot_bury_the_verdict():
    """Thirteen bullets of one-off misses pushed the cross-model table off the
    first screen. They are sorted worst-first, so the tail is the cheap part."""
    results = []
    for i in range(S.TOP_COLLISIONS + 3):
        results.append(fixture_result(f"v{i}", f"victim{i}", "thief", False))
    results.append(fixture_result("t1", "thief", "thief", True))

    text = S.render_tool_scorecard(results, {})
    visible, _, rest = text.partition("<summary>The remaining")

    assert visible.count("miss(es)") == S.TOP_COLLISIONS
    assert rest, "the tail should be behind a disclosure, not dropped"


def test_scorecard_section_flags_a_mutual_pair():
    text = S.render_tool_scorecard(
        [
            fixture_result("a1", "alpha", "beta", False),
            fixture_result("b1", "beta", "alpha", False),
        ],
        {},
    )
    assert "**(mutual)**" in text


def test_scorecard_section_truncates_a_long_steal_list():
    results = [fixture_result(f"v{i}", f"victim{i}", "greedy", False) for i in range(4)]
    text = S.render_tool_scorecard(results, {})
    assert "+2" in text


def test_render_includes_the_scorecard_when_results_are_supplied():
    text = S.render(
        [row("admin · primary", "gpt-4o", 0.9, 1)],
        None,
        [fixture_result("a1", "alpha", "alpha", True)],
        {},
    )
    assert "### Per-tool scorecard" in text


def test_render_omits_the_scorecard_when_no_reports_were_named():
    text = S.render([row("admin · primary", "gpt-4o", 0.9, 1)], None)
    assert "### Per-tool scorecard" not in text


# ---------------------------------------------------------------------------
# Cost headline
# ---------------------------------------------------------------------------


def cost_block(total=1.5, graded=10, passed=9, **over):
    return {
        "model": "gpt-5.4-mini",
        "priced": True,
        "unverified": False,
        "verified": "2026-07-30",
        "tokens": {"input": 1_500_000, "cached_input": 200_000, "output": 12_000},
        "total_usd": total,
        "graded": graded,
        "passed": passed,
        **over,
    }


def test_cost_section_reports_the_total_and_both_unit_prices():
    rows = [row("admin · primary", "gpt-5.4-mini", 0.9, 10, cost=cost_block())]

    out = S.render_cost(rows)

    assert "$1.50" in out
    assert "1.5M input (200k cached)" in out
    assert "per fixture" in out and "per *passing* fixture" in out
    assert "Prices last verified 2026-07-30" in out


def test_cost_per_passing_fixture_is_dearer_than_cost_per_fixture_when_some_fail():
    """Sanity on the arithmetic: fewer passes than fixtures means a higher unit
    price for signal, which is the whole reason the second number exists."""
    out = S.render_cost([row("s", "m", 0.5, 10, cost=cost_block(total=1.0, graded=10, passed=5))])

    assert "$0.1000 per fixture" in out
    assert "$0.2000 per *passing* fixture" in out


def test_an_unpriced_model_shows_a_gap_in_the_table_not_a_zero():
    """$0.00 would read as free rather than as unknown."""
    rows = [row("s", "mystery", 0.9, 10, cost=cost_block(total=None, priced=False))]

    assert "unpriced" in S.render_runs(rows)
    assert "$0.00" not in S.render_runs(rows)


def test_a_row_with_no_cost_block_renders_a_dash():
    assert "| — |" in S.render_runs([row("s", "m", 0.9, 10)])


def test_the_job_total_says_so_when_a_price_is_missing():
    rows = [
        row("a", "m1", 0.9, 10, cost=cost_block()),
        row("b", "mystery", 0.9, 10, cost=cost_block(total=None, model="mystery")),
    ]

    out = S.render_cost(rows)

    assert "incomplete" in out and "mystery" in out
    assert "3.0M input" in out, "token volume is known even when the price is not"


def test_estimated_rates_are_marked_in_the_table_and_footnoted():
    rows = [row("a", "gpt-5.4-mini", 0.9, 10, cost=cost_block(unverified=True))]

    assert "$1.50*" in S.render_runs(rows)
    assert "estimated rates for gpt-5.4-mini" in S.render_cost(rows)


def test_cost_section_is_omitted_when_no_run_reported_one():
    assert S.render_cost([row("a", "m", 0.9, 10)]) == ""
    assert S.render_cost([]) == ""


# ---------------------------------------------------------------------------
# Arm matrix
# ---------------------------------------------------------------------------


def arm_report(**arms):
    return {"modes": {arm: {"results": results} for arm, results in arms.items()}}


def outcome(fid, passed, expected="alpha"):
    return {"id": fid, "passed": passed, "expected_tool": expected}


def test_arm_matrix_separates_the_three_causes_of_one_symptom():
    """All three failed the discovery arm identically. The arms say which
    problem produced that symptom, which is the difference between a rewrite,
    a pair to differentiate, and a group description."""
    reports = [
        arm_report(
            discovery=[outcome("own", False), outcome("collide", False), outcome("layer", False)],
            isolated=[outcome("own", False), outcome("collide", True), outcome("layer", True)],
            full=[outcome("own", False), outcome("collide", False), outcome("layer", True)],
        )
    ]

    out = S.render_arm_matrix(reports)

    assert "| `own` | `alpha` | ✗ | ✗ | the tool's own description |" in out
    assert "| `collide` | `alpha` | ✓ | ✗ | a cross-group collision |" in out
    assert "| `layer` | `alpha` | ✓ | ✓ | the discovery layer |" in out


def test_arm_matrix_counts_each_bucket_and_says_what_to_do():
    reports = [
        arm_report(
            isolated=[outcome("a", True), outcome("b", True)],
            full=[outcome("a", True), outcome("b", True)],
        )
    ]

    out = S.render_arm_matrix(reports)

    assert "**the discovery layer** (2)" in out
    assert "McpToolGroup" in out


def test_arm_matrix_states_that_passing_fixtures_were_not_re_run():
    """A silent cap reads as full coverage when it is not."""
    reports = [arm_report(isolated=[outcome("a", True)], full=[outcome("a", True)])]

    assert "Triaged 1 discovery failures" in S.render_arm_matrix(reports)


def test_arm_matrix_is_omitted_when_triage_did_not_run():
    assert S.render_arm_matrix([arm_report(discovery=[outcome("a", False)])]) == ""
    assert S.render_arm_matrix([]) == ""


def test_a_fixture_only_one_arm_reached_is_not_diagnosed():
    """Both arms are needed to place a failure; one alone cannot."""
    reports = [arm_report(isolated=[outcome("a", True)], full=[])]

    out = S.render_arm_matrix(reports)

    assert "`a`" not in out


def test_render_includes_the_arm_matrix_when_reports_carry_one():
    text = S.render(
        [row("admin · primary", "m", 0.9, 1)],
        None,
        None,
        None,
        [arm_report(isolated=[outcome("a", True)], full=[outcome("a", True)])],
    )
    # Nested under a disclosure now, so its own `###` heading is dropped — a
    # <summary> label with the same words directly beneath it reads as a bug.
    assert "<summary>Triage — which arm located each failure</summary>" in text
    assert "### Where the failures are" not in text
    assert "| Fixture | Expected |" in text, "the table itself still ships"


def test_an_arm_that_never_advertised_the_tool_is_not_diagnosed():
    """The regression this guard exists for.

    The first Store triage produced an empty surface for all 16 fixtures — the
    toolset name did not match, so nothing was enabled. Without this, the matrix
    read every one as "the tool's own description": a confident answer to a
    question that was never asked.
    """
    reports = [
        arm_report(
            isolated=[
                {
                    "id": "broken",
                    "passed": False,
                    "expected_tool": "alpha",
                    "skipped": True,
                    "skip_reason": "arm setup failed: alpha was not advertised",
                },
                outcome("real", False),
            ],
            full=[outcome("broken", False), outcome("real", False)],
        )
    ]

    out = S.render_arm_matrix(reports)

    assert "could not be diagnosed" in out
    assert "arm setup failed" in out
    assert "| `broken` |" not in out, "it must not appear in the diagnosis table"
    assert "| `real` |" in out
    assert "2 discovery failures, 1 diagnosed" in out


def test_a_fully_unusable_triage_diagnoses_nothing():
    reports = [
        arm_report(
            isolated=[
                {"id": "a", "passed": False, "expected_tool": "alpha", "skipped": True, "skip_reason": "no surface"}
            ],
            full=[{"id": "a", "passed": False, "expected_tool": "alpha", "skipped": True, "skip_reason": "no surface"}],
        )
    ]

    out = S.render_arm_matrix(reports)

    assert "1 discovery failures, 0 diagnosed" in out
    assert "the tool's own description" not in out


def test_module_runs_as_a_script_without_nameerror(tmp_path):
    """The entrypoint must resolve every renderer it calls.

    Importing the module defines every function regardless of order, so the
    other tests here never exercise `main()`. Running it as `python -m
    eval.summary` does, and a renderer defined below the `__main__` guard was
    undefined at that point — a NameError that only ever surfaced in CI, with
    the whole job summary as the cost. This runs the real entrypoint in a
    subprocess so that ordering regression fails a unit test instead.
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "eval.summary", "--rows", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "NameError" not in proc.stderr
    assert "## MCP evals" in proc.stdout


# ---------------------------------------------------------------------------
# Reading the page: what stays in front of the reader, and how prose wraps.
#
# GitHub renders a single newline in a step summary as a line break, so prose
# hard-wrapped in the source arrived wrapped at ~70 columns inside a browser
# column three times that wide. Measured before this: 151 of 160 lines visible,
# with the verdict at 738 of 9,470 bytes.
# ---------------------------------------------------------------------------
def test_para_joins_sentences_into_one_line_so_the_browser_wraps_it():
    assert S.para("first half", "second half") == "first half second half"
    assert "\n" not in S.para("a", "b", "c")


def test_para_drops_blanks_rather_than_leaving_double_spaces():
    assert S.para("a", "", "  ", "b") == "a b"


def test_details_needs_the_blank_line_after_summary_to_render_markdown():
    """Without it GitHub prints the markdown inside as literal text."""
    out = S.details("Label", "| a | b |\n|---|---|")

    assert out.startswith("<details>\n<summary>Label</summary>\n\n")
    assert out.endswith("</details>\n")


def test_details_of_nothing_renders_nothing():
    """An empty disclosure is worse than an absent one — it invites a click that
    reveals a blank."""
    assert S.details("Label", "") == ""
    assert S.details("Label", "   \n  ") == ""


def test_nest_drops_the_inner_heading_but_keeps_the_body():
    out = S.nest("Outer label", "### Inner heading\n\nbody text")

    assert "Inner heading" not in out
    assert "body text" in out


def test_no_prose_line_in_the_rendered_page_is_source_wrapped():
    """The regression guard for the wrapping. A prose line that stops well short
    of any sane column is a source line ending that leaked into the output."""
    text = S.render(
        [row("admin · primary", "m", 0.9, 1)],
        None,
        [fixture_result("a1", "alpha", "beta", False)],
        None,
        None,
    )
    stumps = [
        line
        for line in text.split("\n")
        # Prose only: tables, bullets, headings and HTML wrap on their own terms.
        if 30 < len(line) < 75 and not line.startswith(("|", "-", "#", "<", "*", " ", "$"))
    ]

    assert not stumps, f"hard-wrapped prose leaked into the summary: {stumps}"


# ---------------------------------------------------------------------------
# Catalogue listings — both endpoints or neither.
#
# The Store listing used to be echoed straight into the static job's summary
# with no admin equivalent, so the run page documented the optional plugin's
# tools and said nothing about the 30 that ship in core.
# ---------------------------------------------------------------------------
SNAPSHOT = {
    "tools": [{"name": "alpha"}, {"name": "beta"}],
    "toolsets": [{"name": "group", "tools": ["alpha", "beta"]}],
}


def test_a_catalogue_renders_its_toolsets_collapsed():
    out = S.render_catalogue(SNAPSHOT, "Admin")

    assert "<summary>Admin catalogue — 2 tools</summary>" in out
    assert "2 tools in 1 toolsets" in out
    assert "- **group** — `alpha`, `beta`" in out


def test_an_absent_snapshot_omits_its_section_rather_than_showing_an_empty_one():
    """store.json is missing on any run whose preflight did not get that far."""
    assert S.render_catalogue(None, "Store") == ""
    assert S.render_catalogue({}, "Store") == ""


def test_both_catalogues_appear_when_both_snapshots_exist():
    text = S.render(
        [row("admin · primary", "m", 0.9, 1)],
        None,
        None,
        None,
        None,
        admin_snapshot=SNAPSHOT,
        store_snapshot=SNAPSHOT,
    )

    assert "Admin catalogue" in text
    assert "Store catalogue" in text


def test_the_catalogue_lint_is_rendered_here_not_in_a_second_workflow():
    """toollint is pure, so running it again in this job is free and is what
    lets one page carry the whole picture."""
    text = S.render([row("admin · primary", "m", 0.9, 1)], None, None, None, None, admin_snapshot=SNAPSHOT)

    assert "<summary>Tool catalogue lint — static description findings</summary>" in text
    # Its own H2 must not compete with this page's.
    assert "## Tool catalogue lint" not in text
