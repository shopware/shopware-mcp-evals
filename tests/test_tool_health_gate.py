"""The gate that keeps LLM budget off tools already proven broken.

The rule it encodes: a tool the static layer could not run is an upstream bug,
not a description problem. Grading a model on finding it charges that bug to the
model, and pays full model price to rediscover what one direct call established.

The risk it introduces is the opposite one — a suite that quietly stops testing
things and reports a better number for it. So these tests care as much about what
stays graded, and about the skip being visible, as about what gets withheld.
"""

import json
from pathlib import Path
from typing import cast

import pytest

from eval import runner, summary
from eval.result_schema import Fixture, JsonObject, Report, SkippedFixture, ToolHealth
from tests.stubs import never


def _fixture(tool: str, **over: object) -> Fixture:
    base: JsonObject = {
        "id": "f1",
        "prompt": "p",
        "expected_tool": tool,
        "category": "unambiguous",
        **over,
    }
    return cast(Fixture, cast(object, base))


def _skipped_report(*skipped: SkippedFixture) -> Report:
    return cast(Report, cast(object, {"skipped_fixtures": list(skipped)}))


def test_only_a_proven_failure_withholds_fixtures() -> None:
    """`skipped` means unproven, not broken — a tool may be unsafe to call, or
    its journey step never reached. Withholding on that basis would shrink the
    suite every time the static layer got more cautious."""
    health = {
        "broken": ToolHealth(status="fail", reason="validation: $.payment is required"),
        "unproven": ToolHealth(status="skipped", reason="precondition missing: order_id"),
        "fine": ToolHealth(status="pass"),
    }

    assert "$.payment is required" in runner.unhealthy_reason("broken", health)
    assert runner.unhealthy_reason("unproven", health) == ""
    assert runner.unhealthy_reason("fine", health) == ""


def test_a_tool_with_no_verdict_is_still_graded() -> None:
    """Absence of evidence is not evidence of breakage. A tool the static layer
    never looked at keeps its fixtures."""
    assert runner.unhealthy_reason("never-seen", {"other": ToolHealth(status="fail")}) == ""


def test_a_negative_fixture_is_never_withheld() -> None:
    """It names no tool — the whole point is that nothing should be called — so
    there is no health verdict that could apply to it."""
    assert runner.unhealthy_reason(None, {"x": {"status": "fail"}}) == ""


def test_missing_health_file_grades_everything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A local run without the functional suite must still work. Missing means
    "no evidence", not "nothing works"."""
    assert runner.load_tool_health(None) == {}
    assert runner.load_tool_health(str(tmp_path / "absent.json")) == {}
    assert "could not read tool health" in capsys.readouterr().out


def test_unreadable_health_file_warns_and_grades_everything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The gate must never be the thing that stops a run."""
    path = tmp_path / "broken.json"
    path.write_text("{not json")

    assert runner.load_tool_health(str(path)) == {}
    assert "could not read tool health" in capsys.readouterr().out


def test_a_health_file_that_is_not_a_map_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps(["nonsense"]))

    assert runner.load_tool_health(str(path)) == {}


def test_the_skip_records_the_reason_not_just_the_fact() -> None:
    """A shrinking denominator has to be explainable. "skipped" alone is
    indistinguishable from the pre-existing not-registered case."""
    fixture = _fixture("broken")

    result = runner.skipped_result(fixture, "discovery", "static checks failed for this tool: boom")

    assert result.get("skipped") is True
    assert result["passed"] is False
    assert "boom" in result.get("skip_reason", "")


def test_the_default_reason_still_describes_the_original_case() -> None:
    fixture = _fixture("gone")

    assert "not registered" in runner.skipped_result(fixture, "discovery").get("skip_reason", "")


def test_a_fixture_the_lane_could_not_supply_an_id_for_is_skipped_not_graded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule as a broken tool, different missing piece. The prompt names an
    id the lane could not resolve, so the call the model would make cannot
    succeed — grading it charges the lane's gap to the model. That is precisely
    what the three cart fixtures did: merchant-cart-checkout named correctly
    every time, marked wrong for a token invented in the YAML."""
    fixture = _fixture(
        "merchant-cart-checkout",
        id="cart_checkout_place",
        prompt="check out cart {cart_token}",
        unresolved_placeholder="cart_token",
    )
    monkeypatch.setattr(runner, "run_fixture_discovery", never("the model was asked"))

    results = runner.run_discovery_pass("openai", None, [fixture], "m", None, 6, {"merchant-cart-checkout"}, workers=1)

    assert results[0].get("skipped") is True
    assert results[0].get("skip_reason") == "lane could not resolve {cart_token}"


def test_skipped_fixtures_are_rendered_grouped_by_reason() -> None:
    """Rendered next to the rate, because that is the number they change."""
    reports = [
        _skipped_report(
            SkippedFixture(id="a", expected_tool="shopware-ucp-checkout-complete", reason="static checks failed"),
            SkippedFixture(id="b", expected_tool="shopware-ucp-checkout-complete", reason="static checks failed"),
            SkippedFixture(id="c", expected_tool="swag-dev-tools-scaffold", reason="not registered"),
        )
    ]

    out = summary.render_skipped(reports)

    assert "Not graded (3 fixtures)" in out
    assert "shopware-ucp-checkout-complete" in out
    # Most-skipped reason first, so the biggest hole is the first thing read.
    assert out.index("static checks failed") < out.index("not registered")


def test_nothing_skipped_renders_nothing() -> None:
    """No empty section on a clean run — it would train people to skip past it."""
    assert summary.render_skipped([_skipped_report()]) == ""
    assert summary.render_skipped([]) == ""
