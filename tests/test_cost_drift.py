"""Cost drift between two runs.

The comparison has to be per fixture and it has to be non-gating; both are easy
to get wrong in ways that look fine. Comparing totals would report a grown suite
as a regression, and gating on it would turn a provider-side tokenization change
into a red build nobody can fix.
"""

import json

import pytest

from eval import cost_drift as D


def report(graded=100, input_tokens=1_000_000, cached=0, output=None, **cost_extra):
    # Output scales with the fixture count by default, so "the suite grew" is
    # genuinely a same-cost-per-fixture scenario rather than one that halves
    # output per fixture and trips the very check being tested.
    return {
        "cost": {
            "graded": graded,
            "tokens": {
                "input": input_tokens,
                "cached_input": cached,
                "output": graded * 100 if output is None else output,
            },
            **cost_extra,
        }
    }


def test_a_suite_that_grew_is_not_a_regression():
    """The reason everything is per fixture: twice the fixtures at the same cost
    each is twice the total and no change at all in what is being measured."""
    before = report(graded=100, input_tokens=1_000_000)
    after = report(graded=200, input_tokens=2_000_000)

    assert D.compare(after, before) == []


def test_more_context_per_fixture_is_reported():
    findings = D.compare(report(input_tokens=2_000_000), report(input_tokens=1_000_000))

    assert [f["metric"] for f in findings] == ["input_tokens_per_fixture"]
    assert findings[0]["change"] == pytest.approx(1.0)
    assert "more context" in findings[0]["meaning"]


def test_cached_tokens_count_toward_context_growth():
    """What moved is how much context the model was handed, not what it was
    billed for — otherwise a run that merely started hitting the cache would
    read as a 100% improvement."""
    before = report(input_tokens=1_000_000, cached=0)
    after = report(input_tokens=100_000, cached=900_000)

    assert D.compare(after, before) == []


def test_a_change_below_the_threshold_is_noise():
    assert D.compare(report(input_tokens=1_100_000), report(input_tokens=1_000_000)) == []


def test_a_large_drop_is_reported_too():
    """A sudden halving is the shape a silently-broken run takes — fewer steps
    because discovery stopped happening at all."""
    findings = D.compare(report(input_tokens=200_000), report(input_tokens=1_000_000))

    assert findings[0]["change"] == pytest.approx(-0.8)
    assert "▼" in D.render(findings)


def test_payload_and_surface_growth_are_tracked_separately():
    before = report(payload_bytes_p50=100, surface_tokens_peak=200)
    after = report(payload_bytes_p50=1000, surface_tokens_peak=800)

    metrics = {f["metric"] for f in D.compare(after, before)}

    assert metrics == {"payload_bytes_p50", "surface_tokens_peak"}


def test_findings_are_ordered_by_how_much_moved():
    before = report(input_tokens=1_000_000, payload_bytes_p50=100)
    after = report(input_tokens=1_500_000, payload_bytes_p50=1000)

    assert [f["metric"] for f in D.compare(after, before)][0] == "payload_bytes_p50"


def test_a_field_the_older_report_predates_is_not_compared_against_zero():
    """Reports written before a field existed must produce no comparison rather
    than a fabricated one."""
    assert D.compare(report(payload_bytes_p50=500), report()) == []
    assert D.compare(report(), report(payload_bytes_p50=500)) == []


def test_a_report_with_no_graded_fixtures_yields_no_metrics():
    assert D.metrics(report(graded=0)) == {}
    assert D.metrics({}) == {}


def test_render_says_so_when_nothing_moved():
    assert "within 25%" in D.render([])


def test_render_lists_what_moved_and_what_it_means():
    out = D.render(D.compare(report(input_tokens=2_000_000), report(input_tokens=1_000_000)))

    assert "input tokens per fixture" in out
    assert "▲ 100%" in out
    assert "more context" in out


# ---------------------------------------------------------------------------
# CLI — advisory in every branch
# ---------------------------------------------------------------------------
def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def test_a_regression_warns_but_does_not_fail(tmp_path, monkeypatch, capsys):
    """Provider-side changes move these numbers through no fault of the server.
    A red build for that is one people learn to bypass."""
    current = write(tmp_path, "now.json", report(input_tokens=3_000_000))
    previous = write(tmp_path, "before.json", report(input_tokens=1_000_000))
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", current, "--previous", previous])

    assert D.main() == 0
    captured = capsys.readouterr()
    assert "::warning::" in captured.err
    assert "input tokens per fixture" in captured.out


def test_an_improvement_is_shown_but_not_warned_about(tmp_path, monkeypatch, capsys):
    current = write(tmp_path, "now.json", report(input_tokens=200_000))
    previous = write(tmp_path, "before.json", report(input_tokens=1_000_000))
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", current, "--previous", previous])

    assert D.main() == 0
    captured = capsys.readouterr()
    assert "::warning::" not in captured.err
    assert "▼" in captured.out


def test_no_previous_report_is_a_normal_first_run_not_a_warning(tmp_path, monkeypatch, capsys):
    current = write(tmp_path, "now.json", report())
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", current])

    assert D.main() == 0
    captured = capsys.readouterr()
    assert "cost drift skipped" in captured.out
    assert "::warning::" not in captured.err


def test_an_unreadable_previous_report_is_also_skipped(tmp_path, monkeypatch, capsys):
    current = write(tmp_path, "now.json", report())
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", current, "--previous", str(tmp_path / "gone.json")])

    assert D.main() == 0
    assert "cost drift skipped" in capsys.readouterr().out


def test_an_unreadable_current_report_warns_and_still_exits_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", str(tmp_path / "gone.json")])

    assert D.main() == 0
    assert "::warning::" in capsys.readouterr().err


def test_the_threshold_is_configurable(tmp_path, monkeypatch, capsys):
    current = write(tmp_path, "now.json", report(input_tokens=1_100_000))
    previous = write(tmp_path, "before.json", report(input_tokens=1_000_000))
    monkeypatch.setattr("sys.argv", ["cost_drift", "--current", current, "--previous", previous, "--threshold", "0.05"])

    assert D.main() == 0
    assert "input tokens per fixture" in capsys.readouterr().out
