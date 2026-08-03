"""The gate block the runner prints, and client construction.

`print_gate` reads only the verdict dict, so gate_verdict stays the single place
the decision is made — these check that the three independent failure modes each
get said out loud, because a run that fails on validity rather than quality needs
a different response from the reader.
"""

import argparse
import re
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from eval import runner as E
from eval.result_schema import FixtureResult, GateVerdict, JsonObject, PromptInventory, ToolDef
from mcp_client import Endpoint
from tests.stubs import const, never

STRIP = re.compile(r"\033\[[0-9;]*m")


# Any endpoint: every server call below is stubbed.
EP: Endpoint = E.ADMIN


def session(endpoint: Endpoint | None = None) -> tuple[str, str]:
    """mcp_init, without a server. Keeps the keyword name the runner passes."""
    assert endpoint is None or endpoint is EP
    return "sid", ""


def plain(capsys: pytest.CaptureFixture[str]) -> str:
    return STRIP.sub("", capsys.readouterr().out)


def args(min_pass_rate: float = 0.9, max_error_rate: float = 0.1) -> argparse.Namespace:
    """The two thresholds print_gate reads off the parsed arguments."""
    return argparse.Namespace(min_pass_rate=min_pass_rate, max_error_rate=max_error_rate)


def r(fid: str, passed: bool = True, tool: str = "shopware-entity-read", **over: object) -> FixtureResult:
    base: JsonObject = {"id": fid, "passed": passed, "expected_tool": tool, "category": "unambiguous", **over}
    return cast(FixtureResult, cast(object, base))


def verdict(
    results: list[FixtureResult],
    min_pass_rate: float = 0.9,
    min_core_pass_rate: float | None = None,
    max_error_rate: float = 0.1,
) -> GateVerdict:
    return E.gate_verdict(results, min_pass_rate, min_core_pass_rate, max_error_rate)


# ---------------------------------------------------------------------------
# print_gate
# ---------------------------------------------------------------------------
def test_gate_block_reports_a_pass(capsys: pytest.CaptureFixture[str]) -> None:
    E.print_gate(verdict([r(f"p{i}") for i in range(10)]), args())
    out = plain(capsys)

    assert "Gate: 10/10 = 100% (threshold 90%) → PASS" in out


def test_gate_block_names_the_failing_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    """Without the ids the only way to find them is the artifact."""
    E.print_gate(verdict([r("ok"), r("bad1", passed=False), r("bad2", passed=False)]), args())
    out = plain(capsys)

    assert "→ FAIL" in out
    assert "below threshold; failing: bad1, bad2" in out


def test_gate_block_reports_the_core_gate_separately(capsys: pytest.CaptureFixture[str]) -> None:
    results = [r("c", passed=False, tool="shopware-entity-read"), r("m", tool="merchant-order-summary")]

    E.print_gate(verdict(results, min_pass_rate=0.5), args(min_pass_rate=0.5))
    out = plain(capsys)

    assert "Core gate: 0/1 = 0%" in out
    assert "core below threshold; failing: c" in out


def test_gate_block_omits_the_core_line_when_no_core_fixtures_ran(capsys: pytest.CaptureFixture[str]) -> None:
    """The store suite is almost all UCP and has no core denominator."""
    E.print_gate(verdict([r("u", tool="shopware-ucp-cart-get")], min_pass_rate=0.0), args(min_pass_rate=0.0))

    assert "Core gate" not in plain(capsys)


def test_gate_block_reports_errors_within_budget_as_such(capsys: pytest.CaptureFixture[str]) -> None:
    results = [r(f"p{i}") for i in range(9)] + [r("e", passed=False, error="500")]

    E.print_gate(verdict(results), args())
    out = plain(capsys)

    assert "1/10 fixtures never reached the model (10%, budget 10%) → within budget" in out


def test_gate_block_calls_out_an_invalid_run_distinctly_from_a_bad_one(capsys: pytest.CaptureFixture[str]) -> None:
    """ "RUN INVALID" means fix the server and re-run; "FAIL" means the model got
    it wrong. Conflating them sends you debugging the wrong thing."""
    results = [r("p")] + [r(f"e{i}", passed=False, error="500") for i in range(9)]

    E.print_gate(verdict(results), args())
    out = plain(capsys)

    assert "RUN INVALID" in out
    assert "too many fixtures errored to trust this run" in out


def test_gate_block_omits_the_error_line_on_a_clean_run(capsys: pytest.CaptureFixture[str]) -> None:
    E.print_gate(verdict([r("a")]), args())

    assert "never reached the model" not in plain(capsys)


def test_gate_block_includes_the_by_owner_table_when_owners_differ(capsys: pytest.CaptureFixture[str]) -> None:
    results = [r("c", tool="shopware-entity-read"), r("d", tool="swag-dev-tools-log-search")]

    E.print_gate(verdict(results), args())

    assert "By owner:" in plain(capsys)


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------
def test_github_provider_points_at_the_github_models_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """It speaks the OpenAI wire format, so only the base URL and credential
    differ — pointing it at api.openai.com would authenticate with the wrong key."""
    captured: JsonObject = {}

    class FakeOpenAI:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    E.build_client("github", ("GITHUB_TOKEN", "ghs_x"))

    assert captured == {"api_key": "ghs_x", "base_url": E.GITHUB_MODELS_BASE_URL}


def test_openai_provider_uses_the_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: JsonObject = {}

    class FakeOpenAI:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    E.build_client("openai", ("OPENAI_API_KEY", "sk-x"))

    assert captured["base_url"] is None


def test_anthropic_provider_builds_an_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: JsonObject = {}

    class FakeAnthropic:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))

    E.build_client("anthropic", ("ANTHROPIC_API_KEY", "sk-ant"))

    assert captured == {"api_key": "sk-ant"}


# ---------------------------------------------------------------------------
# probe_catalogue / fetch_system_prompt
# ---------------------------------------------------------------------------
def test_probe_catalogue_enables_everything_before_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probing the default surface would report only the three meta-tools, and
    every deferred fixture would then be skipped as 'tool not registered'."""
    order: list[str] = []

    def enable_all(_session: str, endpoint: Endpoint | None = None) -> list[str]:
        assert endpoint is None or endpoint is EP
        order.append("enable")
        return []

    def list_all(_session: str, endpoint: Endpoint | None = None) -> list[ToolDef]:
        assert endpoint is None or endpoint is EP
        order.append("list")
        return [ToolDef(name="a"), ToolDef(name="b")]

    monkeypatch.setattr(E, "mcp_init", session)
    monkeypatch.setattr(E, "enable_all_toolsets", enable_all)
    monkeypatch.setattr(E, "mcp_tools_list_all", list_all)

    assert E.probe_catalogue(EP) == {"a", "b"}
    assert order == ["enable", "list"]


def test_context_prompt_can_be_disabled_for_debugging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(E, "mcp_init", never("must not open a session"))

    prompt, inventory = E.fetch_system_prompt(EP, enabled=False)

    assert prompt is None
    # Recorded as disabled rather than as an empty prompt: "we turned it off" and
    # "the server served nothing" are different facts, and the store endpoint
    # genuinely is the second one.
    assert inventory.get("disabled") is True
    assert "Context prompt: none" in capsys.readouterr().out


def test_context_prompt_reports_the_inventory_it_fetched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A boolean cannot distinguish two runs whose prompt *content* differs, and
    it cannot show that admin serves four prompts while store serves none."""
    monkeypatch.setattr(E, "mcp_init", const(("sid", "instructions")))
    inventory_served = PromptInventory(
        names=["shopware-context", "merchant-context"],
        chars={},
        total_chars=20,
        sha256="abc",
        excluded=[],
    )
    monkeypatch.setattr(E, "mcp_fetch_context_prompts", const(("# One\nbody\n# Two\nbody", inventory_served)))

    prompt, inventory = E.fetch_system_prompt(EP, enabled=True)

    assert prompt is not None and prompt.startswith("# One")
    assert inventory["names"] == ["shopware-context", "merchant-context"]
    out = capsys.readouterr().out
    assert "20 chars from 2" in out
    assert "shopware-context" in out


def test_lmstudio_points_at_the_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same adapter and turn function as openai/github — only the base URL and
    credential differ, which is the pattern the github provider already proved."""
    captured: JsonObject = {}

    class FakeOpenAI:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    E.build_client("lmstudio", ("LMSTUDIO_API_KEY", "lm-studio"))

    assert captured == {"api_key": "lm-studio", "base_url": E.LMSTUDIO_BASE_URL}


def test_lmstudio_needs_no_real_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local server wants no key, but the SDK requires one and
    require_credentials rejects an empty string. Defaulting it keeps a free local
    run from failing on a secret that does not exist."""
    monkeypatch.setattr(E, "SW_BASE_URL", "http://x")
    monkeypatch.setattr(E, "SW_SC_ACCESS_KEY", "k")

    name, value = E.require_credentials("lmstudio", "store")

    assert name == "LMSTUDIO_API_KEY"
    assert value, "an empty default would make every local run fail on credentials"


def test_the_local_model_is_priced_at_zero_not_left_unpriced() -> None:
    """It genuinely is free, so $0.00 is the true rate. "unpriced" would read as
    unknown cost and put the run in the incomplete bucket."""
    from eval.cost import load_pricing, prices_for

    prices = prices_for(E.PROVIDER_DEFAULTS["lmstudio"], load_pricing())

    assert prices is not None
    assert prices.get("input") == 0.0 and prices.get("output") == 0.0
