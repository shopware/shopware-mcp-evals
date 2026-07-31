"""The gate block the runner prints, and client construction.

`print_gate` reads only the verdict dict, so gate_verdict stays the single place
the decision is made — these check that the three independent failure modes each
get said out loud, because a run that fails on validity rather than quality needs
a different response from the reader.
"""

import re
from types import SimpleNamespace

import pytest

from eval import runner as E

STRIP = re.compile(r"\033\[[0-9;]*m")


def plain(capsys):
    return STRIP.sub("", capsys.readouterr().out)


def args(min_pass_rate=0.9, max_error_rate=0.1):
    return SimpleNamespace(min_pass_rate=min_pass_rate, max_error_rate=max_error_rate)


def r(fid, passed=True, tool="shopware-entity-read", **over):
    return {"id": fid, "passed": passed, "expected_tool": tool, "category": "unambiguous", **over}


def verdict(results, **kw):
    return E.gate_verdict(
        results,
        kw.get("min_pass_rate", 0.9),
        kw.get("min_core_pass_rate"),
        kw.get("max_error_rate", 0.1),
    )


# ---------------------------------------------------------------------------
# print_gate
# ---------------------------------------------------------------------------
def test_gate_block_reports_a_pass(capsys):
    E.print_gate(verdict([r(f"p{i}") for i in range(10)]), args())
    out = plain(capsys)

    assert "Gate: 10/10 = 100% (threshold 90%) → PASS" in out


def test_gate_block_names_the_failing_fixtures(capsys):
    """Without the ids the only way to find them is the artifact."""
    E.print_gate(verdict([r("ok"), r("bad1", passed=False), r("bad2", passed=False)]), args())
    out = plain(capsys)

    assert "→ FAIL" in out
    assert "below threshold; failing: bad1, bad2" in out


def test_gate_block_reports_the_core_gate_separately(capsys):
    results = [r("c", passed=False, tool="shopware-entity-read"), r("m", tool="merchant-order-summary")]

    E.print_gate(verdict(results, min_pass_rate=0.5), args(min_pass_rate=0.5))
    out = plain(capsys)

    assert "Core gate: 0/1 = 0%" in out
    assert "core below threshold; failing: c" in out


def test_gate_block_omits_the_core_line_when_no_core_fixtures_ran(capsys):
    """The store suite is almost all UCP and has no core denominator."""
    E.print_gate(verdict([r("u", tool="shopware-ucp-cart-get")], min_pass_rate=0.0), args(min_pass_rate=0.0))

    assert "Core gate" not in plain(capsys)


def test_gate_block_reports_errors_within_budget_as_such(capsys):
    results = [r(f"p{i}") for i in range(9)] + [r("e", passed=False, error="500")]

    E.print_gate(verdict(results), args())
    out = plain(capsys)

    assert "1/10 fixtures never reached the model (10%, budget 10%) → within budget" in out


def test_gate_block_calls_out_an_invalid_run_distinctly_from_a_bad_one(capsys):
    """ "RUN INVALID" means fix the server and re-run; "FAIL" means the model got
    it wrong. Conflating them sends you debugging the wrong thing."""
    results = [r("p")] + [r(f"e{i}", passed=False, error="500") for i in range(9)]

    E.print_gate(verdict(results), args())
    out = plain(capsys)

    assert "RUN INVALID" in out
    assert "too many fixtures errored to trust this run" in out


def test_gate_block_omits_the_error_line_on_a_clean_run(capsys):
    E.print_gate(verdict([r("a")]), args())

    assert "never reached the model" not in plain(capsys)


def test_gate_block_includes_the_by_owner_table_when_owners_differ(capsys):
    results = [r("c", tool="shopware-entity-read"), r("d", tool="swag-dev-tools-log-search")]

    E.print_gate(verdict(results), args())

    assert "By owner:" in plain(capsys)


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------
def test_github_provider_points_at_the_github_models_endpoint(monkeypatch):
    """It speaks the OpenAI wire format, so only the base URL and credential
    differ — pointing it at api.openai.com would authenticate with the wrong key."""
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    E.build_client("github", ("GITHUB_TOKEN", "ghs_x"))

    assert captured == {"api_key": "ghs_x", "base_url": E.GITHUB_MODELS_BASE_URL}


def test_openai_provider_uses_the_default_base_url(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    E.build_client("openai", ("OPENAI_API_KEY", "sk-x"))

    assert captured["base_url"] is None


def test_anthropic_provider_builds_an_anthropic_client(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))

    E.build_client("anthropic", ("ANTHROPIC_API_KEY", "sk-ant"))

    assert captured == {"api_key": "sk-ant"}


# ---------------------------------------------------------------------------
# probe_catalogue / fetch_system_prompt
# ---------------------------------------------------------------------------
def test_probe_catalogue_enables_everything_before_listing(monkeypatch):
    """Probing the default surface would report only the three meta-tools, and
    every deferred fixture would then be skipped as 'tool not registered'."""
    order = []
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: ("sid", ""))
    monkeypatch.setattr(E, "enable_all_toolsets", lambda _s, endpoint=None: order.append("enable"))
    monkeypatch.setattr(
        E, "mcp_tools_list_all", lambda _s, endpoint=None: (order.append("list"), [{"name": "a"}, {"name": "b"}])[1]
    )

    assert E.probe_catalogue(None) == {"a", "b"}
    assert order == ["enable", "list"]


def test_context_prompt_can_be_disabled_for_debugging(monkeypatch, capsys):
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: pytest.fail("must not open a session"))

    prompt, inventory = E.fetch_system_prompt(None, enabled=False)

    assert prompt is None
    # Recorded as disabled rather than as an empty prompt: "we turned it off" and
    # "the server served nothing" are different facts, and the store endpoint
    # genuinely is the second one.
    assert inventory["disabled"] is True
    assert "Context prompt: none" in capsys.readouterr().out


def test_context_prompt_reports_the_inventory_it_fetched(monkeypatch, capsys):
    """A boolean cannot distinguish two runs whose prompt *content* differs, and
    it cannot show that admin serves four prompts while store serves none."""
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: ("sid", "instructions"))
    monkeypatch.setattr(
        E,
        "mcp_fetch_context_prompts",
        lambda *_a, **_k: (
            "# One\nbody\n# Two\nbody",
            {
                "names": ["shopware-context", "merchant-context"],
                "chars": {},
                "total_chars": 20,
                "sha256": "abc",
                "excluded": [],
            },
        ),
    )

    prompt, inventory = E.fetch_system_prompt(None, enabled=True)

    assert prompt.startswith("# One")
    assert inventory["names"] == ["shopware-context", "merchant-context"]
    out = capsys.readouterr().out
    assert "20 chars from 2" in out
    assert "shopware-context" in out
