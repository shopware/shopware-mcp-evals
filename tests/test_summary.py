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
