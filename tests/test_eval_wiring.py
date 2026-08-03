"""The configuration and report-assembly steps of an eval run.

These used to be statements inside a 294-line main(), reachable only by running
the whole suite against a live server and a paid provider. Each one is now a
function that either returns a value or raises ConfigError, so the failure modes
that matter — a typo'd --modes, a missing key, a filter that matches nothing —
are checked here instead of discovered in CI.
"""

import argparse
import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from eval import runner as E
from eval.result_schema import Fixture, FixtureResult, JsonObject, McpResponse, PromptInventory
from mcp_client import Endpoint
from tests.stubs import const, never, raiser

# Any endpoint: every lane call below is stubbed, so only its identity matters.
EP: Endpoint = E.ADMIN


def args(**over: object) -> argparse.Namespace:
    """An argparse.Namespace as build_parser() would produce it, defaults included."""
    return E.build_parser().parse_args([f"--{k.replace('_', '-')}={v}" for k, v in over.items()])


def fx(fid: str, prompt: str, **over: object) -> Fixture:
    base: JsonObject = {"id": fid, "prompt": prompt, **over}
    return cast(Fixture, cast(object, base))


def session(endpoint: Endpoint | None = None) -> tuple[str, str]:
    """mcp_init, without a server. Keeps the `endpoint` keyword name, which the
    runner passes by name."""
    assert endpoint is None or endpoint is EP
    return "sid", ""


def prompt_stub(endpoint: Endpoint, enabled: bool = True, prompt_set: str = "all") -> tuple[str, PromptInventory]:
    """fetch_system_prompt, without a server."""
    assert endpoint is not None
    return "SYSTEM", PromptInventory(names=[], chars={}, total_chars=6, set=prompt_set, disabled=not enabled)


# ---------------------------------------------------------------------------
# parse_modes
# ---------------------------------------------------------------------------
def test_discovery_is_the_only_mode() -> None:
    assert E.parse_modes("discovery") == ["discovery"]


def test_whitespace_and_trailing_commas_are_tolerated() -> None:
    assert E.parse_modes(" discovery , ") == ["discovery"]


def test_baseline_is_rejected_with_an_explanation() -> None:
    """It was a real mode until it turned out to be measuring its own grading, so
    the error has to say so rather than read as a typo."""
    with pytest.raises(E.ConfigError, match="baseline mode was removed"):
        E.parse_modes("baseline,discovery")


def test_a_typod_mode_is_rejected_by_name() -> None:
    """Silently running zero modes would report a vacuous PASS."""
    with pytest.raises(E.ConfigError, match="discovry"):
        E.parse_modes("discovry")


def test_an_empty_mode_list_is_rejected() -> None:
    with pytest.raises(E.ConfigError):
        E.parse_modes(",,")


# ---------------------------------------------------------------------------
# placeholder substitution ({sales_channel_id} -> a real lane id)
# ---------------------------------------------------------------------------
def test_apply_substitutions_replaces_known_tokens_in_place() -> None:
    fixtures = [
        fx("a", "settings for sales channel {sales_channel_id}?"),
        fx("b", "no placeholder here"),
    ]
    E.apply_substitutions(fixtures, {"sales_channel_id": "019e7d92e72d71739401ee2989a47026"})

    assert fixtures[0]["prompt"] == "settings for sales channel 019e7d92e72d71739401ee2989a47026?"
    assert fixtures[1]["prompt"] == "no placeholder here"


def test_resolve_only_probes_placeholders_the_fixtures_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run filtered to fixtures with no placeholder must make no lane calls —
    otherwise every `--id` run pays for a sales_channel lookup it never uses."""
    calls: list[Endpoint] = []

    def resolve(endpoint: Endpoint) -> str:
        calls.append(endpoint)
        return "sc-id"

    monkeypatch.setitem(E.PLACEHOLDER_RESOLVERS, "sales_channel_id", resolve)

    subs = E.resolve_lane_substitutions([fx("x", "plain prompt")], endpoint=EP)

    assert subs == {} and calls == []


def test_resolve_calls_the_resolver_when_the_token_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.PLACEHOLDER_RESOLVERS, "sales_channel_id", const("sc-id"))

    subs = E.resolve_lane_substitutions([fx("x", "for {sales_channel_id}")], endpoint=EP)

    assert subs == {"sales_channel_id": "sc-id"}


@pytest.mark.parametrize(
    "boom",
    [
        ConnectionError("Connection refused"),  # requests raises this (an OSError)
        RuntimeError("No Mcp-Session-Id in response headers"),  # mcp_init, protocol problem
        ValueError("Expecting value: line 1 column 1"),  # a body that is not JSON
    ],
)
def test_a_lane_that_cannot_be_reached_degrades_instead_of_ending_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], boom: Exception
) -> None:
    """A resolver talks to the server, so it fails the way any network call does.
    Letting that propagate kills the run at startup before a single fixture is
    graded — the exact failure mode test_startup_survives_the_real_fixtures was
    written for, reintroduced one layer up. The unresolved placeholder already
    means something (its fixtures are skipped), so degrade to that.
    """

    monkeypatch.setitem(E.PLACEHOLDER_RESOLVERS, "sales_channel_id", raiser(boom))
    fixtures = [fx("x", "for {sales_channel_id}")]

    subs = E.resolve_lane_substitutions(fixtures, endpoint=EP)
    E.apply_substitutions(fixtures, subs)

    assert subs == {}
    assert fixtures[0].get("unresolved_placeholder") == "sales_channel_id"
    assert "resolving {sales_channel_id} off the lane failed" in capsys.readouterr().out


def test_a_seeding_resolver_that_throws_is_caught_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        E.SEEDING_RESOLVERS, ("cart_token", "line_item_id"), raiser(ConnectionError("Connection refused"))
    )

    subs = E.resolve_lane_substitutions([fx("x", "cart {cart_token}")], endpoint=EP, seed_lane=True)

    assert subs == {}
    assert "off the lane failed" in capsys.readouterr().out


def test_an_unresolvable_placeholder_is_left_in_place_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver returns None (no lane / empty shop): the token stays, so the
    fixture fails visibly rather than grading against a literal brace string."""
    monkeypatch.setitem(E.PLACEHOLDER_RESOLVERS, "sales_channel_id", const(None))
    fixtures = [fx("x", "for {sales_channel_id}")]

    E.apply_substitutions(fixtures, E.resolve_lane_substitutions(fixtures, endpoint=EP))

    assert fixtures[0]["prompt"] == "for {sales_channel_id}"


def _fake_lane(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(E, "mcp_init", session)
    monkeypatch.setattr(E, "mcp_call", const(cast(McpResponse, cast(object, {}))))
    monkeypatch.setattr(E, "mcp_result_text", const(text))


def test_first_sales_channel_prefers_the_storefront_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lane(monkeypatch, {"data": [{"id": "aaa", "name": "Headless"}, {"id": "bbb", "name": "Storefront"}]})

    assert E._first_sales_channel_id(EP) == "bbb"


def test_first_sales_channel_falls_back_to_the_first_with_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lane(monkeypatch, {"data": [{"name": "no id here"}, {"id": "ccc", "name": "Music"}]})

    assert E._first_sales_channel_id(EP) == "ccc"


def test_first_sales_channel_is_none_when_the_response_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lane(monkeypatch, "<html>gateway timeout</html>")

    assert E._first_sales_channel_id(EP) is None


# ---------------------------------------------------------------------------
# Seeding placeholders: the ones that cannot be looked up because nothing in a
# fresh shop has one. Held apart from the read-only resolvers and reached only
# under --seed-lane, because creating a cart on somebody's real instance to
# grade a fixture is not a trade this suite gets to make on its own.
# ---------------------------------------------------------------------------
def _cart_fixtures():
    return [fx("checkout", "check out cart {cart_token} for {customer_id}")]


def test_the_entity_resolvers_ask_core_entity_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core, not merchant-*: the fixtures needing a product, customer or order
    id are core fixtures and have to resolve on an instance with no plugins."""
    monkeypatch.setattr(E, "mcp_init", session)

    def call(_session: str, tool: str, arguments: JsonObject, endpoint: object = None) -> McpResponse:
        assert endpoint is EP
        return cast(McpResponse, cast(object, {"_tool": tool, "_args": arguments}))

    def result_text(resp: McpResponse) -> str:
        arguments = cast(JsonObject, cast(object, resp))["_args"]
        entity = cast(JsonObject, arguments)["entity"]
        return json.dumps({"data": [{"id": f"{entity}-1"}]})

    monkeypatch.setattr(E.lane, "mcp_call", call)
    monkeypatch.setattr(E.lane, "mcp_result_text", result_text)

    assert E.PLACEHOLDER_RESOLVERS["product_id"](EP) == "product-1"
    assert E.PLACEHOLDER_RESOLVERS["customer_id"](EP) == "customer-1"
    assert E.PLACEHOLDER_RESOLVERS["order_id"](EP) == "order-1"


def test_an_entity_resolver_returns_none_on_an_empty_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    """None rather than "", so resolve_lane_substitutions warns and the
    fixtures are skipped instead of grading against a literal brace string."""
    monkeypatch.setattr(E, "mcp_init", session)
    monkeypatch.setattr(E.lane, "mcp_call", const(cast(McpResponse, cast(object, {}))))
    monkeypatch.setattr(E.lane, "mcp_result_text", const(json.dumps({"data": []})))

    assert E.PLACEHOLDER_RESOLVERS["product_id"](EP) is None


def test_seeding_opens_one_cart_and_reports_both_of_its_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "mcp_init", session)
    monkeypatch.setattr(E, "_first_sales_channel_id", const("sc-1"))
    monkeypatch.setattr(E.lane, "sellable_products", const(["p1"]))
    seen: list[tuple[object, ...]] = []

    def create_cart(*args: object) -> tuple[str, str]:
        seen.append(args)
        return "tok", "li"

    monkeypatch.setattr(E.lane, "create_cart", create_cart)

    assert E._seed_cart(EP) == {"cart_token": "tok", "line_item_id": "li"}
    assert len(seen) == 1, "one cart, so the line item is in the cart the token names"


def test_seeding_is_skipped_and_announced_when_the_lane_is_not_disposable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(E.PLACEHOLDER_RESOLVERS, "customer_id", const("cus-1"))
    monkeypatch.setitem(E.SEEDING_RESOLVERS, ("cart_token", "line_item_id"), never("wrote to the shop"))

    subs = E.resolve_lane_substitutions(_cart_fixtures(), endpoint=EP, seed_lane=False)

    assert subs == {"customer_id": "cus-1"}, "the read-only resolvers still run"
    assert "Lane seeding off (--seed-lane)" in capsys.readouterr().out


def test_seeding_resolves_every_id_it_provides_from_one_cart(monkeypatch: pytest.MonkeyPatch) -> None:
    """One resolver for both ids because they come from one cart. Two would open
    two, and {line_item_id} would name a line in a cart {cart_token} misses."""
    monkeypatch.setitem(
        E.SEEDING_RESOLVERS, ("cart_token", "line_item_id"), const({"cart_token": "tok", "line_item_id": "li"})
    )
    fixtures = [fx("x", "cart {cart_token} line {line_item_id}")]

    subs = E.resolve_lane_substitutions(fixtures, endpoint=EP, seed_lane=True)

    assert subs == {"cart_token": "tok", "line_item_id": "li"}


def test_a_lane_that_cannot_seed_a_cart_warns_rather_than_substituting_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        E.SEEDING_RESOLVERS, ("cart_token", "line_item_id"), const({"cart_token": "", "line_item_id": ""})
    )
    fixtures = [fx("x", "cart {cart_token}")]

    subs = E.resolve_lane_substitutions(fixtures, endpoint=EP, seed_lane=True)

    assert subs == {}
    assert "could not seed {cart_token}" in capsys.readouterr().out


def test_an_unresolved_placeholder_marks_the_fixture_instead_of_grading_it() -> None:
    """The bug this closes: the model named merchant-cart-checkout correctly on
    all three cart fixtures and was marked wrong, because the token in the
    prompt was invented in the YAML and the server said "Cart is empty"."""
    fixtures = [
        fx("seeded", "cart {cart_token}"),
        fx("fine", "product {product_id}"),
    ]

    E.apply_substitutions(fixtures, {"product_id": "p-1"})

    assert fixtures[0].get("unresolved_placeholder") == "cart_token"
    assert fixtures[1]["prompt"] == "product p-1"
    assert "unresolved_placeholder" not in fixtures[1]


def test_a_brace_that_is_not_a_known_placeholder_is_left_alone() -> None:
    """Only ids this runner knows how to fill count. A fixture author's stray
    brace is their business, and skipping the fixture for it would hide it."""
    fixtures = [fx("x", "set it to {whatever_they_meant}")]

    E.apply_substitutions(fixtures, {})

    assert "unresolved_placeholder" not in fixtures[0]


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------
def test_explicit_model_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_MODEL", "from-env")

    assert E.resolve_model("openai", "from-flag") == "from-flag"


def test_env_model_wins_over_the_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_MODEL", "from-env")

    assert E.resolve_model("openai", None) == "from-env"


def test_provider_default_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_MODEL", raising=False)

    assert E.resolve_model("openai", None) == E.PROVIDER_DEFAULTS["openai"]


# ---------------------------------------------------------------------------
# require_credentials
# ---------------------------------------------------------------------------
def test_admin_run_needs_the_integration_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(E, "ANTHROPIC_API_KEY", "k")

    with pytest.raises(E.ConfigError, match="SW_ACCESS_KEY"):
        E.require_credentials("anthropic", "admin")


def test_store_run_needs_the_sales_channel_key_not_the_integration_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store endpoint authenticates with a sales-channel key; requiring the
    admin pair there would block a perfectly configured store run."""
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(E, "SW_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(E, "SW_SC_ACCESS_KEY", "sc")
    monkeypatch.setattr(E, "OPENAI_API_KEY", "k")

    assert E.require_credentials("openai", "store") == ("OPENAI_API_KEY", "k")


def test_every_missing_variable_is_named_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reporting them one per run means one re-run per missing key."""
    for name in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(E, name, "")

    with pytest.raises(E.ConfigError) as exc:
        E.require_credentials("openai", "admin")

    assert all(n in str(exc.value) for n in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY"))


def test_github_provider_uses_the_workflow_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "GITHUB_TOKEN", "ghs_x")
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "a")
    monkeypatch.setattr(E, "SW_SECRET_ACCESS_KEY", "b")

    assert E.require_credentials("github", "admin") == ("GITHUB_TOKEN", "ghs_x")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def test_store_endpoint_picks_the_store_fixture_file() -> None:
    assert E.fixtures_path_for("store", None).name == "fixtures_store.yaml"
    assert E.fixtures_path_for("admin", None).name == "fixtures.yaml"


def test_an_explicit_fixtures_path_overrides_the_endpoint_default() -> None:
    assert E.fixtures_path_for("store", "/tmp/other.yaml").name == "other.yaml"


def fixture_file(tmp_path: Path, fixtures: list[JsonObject]) -> Path:
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump({"fixtures": fixtures}))
    return p


def test_category_and_id_filters_compose(tmp_path: Path) -> None:
    p = fixture_file(
        tmp_path,
        [
            {"id": "a", "category": "meta", "expected_tool": "t"},
            {"id": "b", "category": "meta", "expected_tool": "t"},
            {"id": "c", "category": "chain", "expected_tool": "t"},
        ],
    )

    assert [f["id"] for f in E.load_fixtures(p, category="meta")] == ["a", "b"]
    assert [f["id"] for f in E.load_fixtures(p, fixture_id="c")] == ["c"]
    assert E.load_fixtures(p, category="meta", fixture_id="a")[0]["id"] == "a"


def test_a_filter_that_matches_nothing_is_an_error_not_an_empty_pass(tmp_path: Path) -> None:
    """An empty fixture set would otherwise score 0/0 and gate as PASS."""
    p = fixture_file(tmp_path, [{"id": "a", "category": "meta", "expected_tool": "t"}])

    with pytest.raises(E.ConfigError, match="No fixtures matched"):
        E.load_fixtures(p, fixture_id="nope")


def test_the_real_fixture_files_load_through_this_path() -> None:
    """Guards the packaged-data path: fixtures live beside the runner, and an
    install that dropped the YAML would only surface here."""
    for endpoint, expected in (("admin", 90), ("store", 42)):
        fixtures = E.load_fixtures(E.fixtures_path_for(endpoint, None))
        assert len(fixtures) >= expected // 2
        # Negative fixtures name no tool — the flag is what identifies them.
        assert all("expected_tool" in f or f.get("expect_no_tool") for f in fixtures)


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------
def result(fid: str, passed: bool = True, tool: str = "shopware-entity-read", **over: object) -> FixtureResult:
    """A discovery result record. The discovery fields are not optional —
    discovery_summary indexes them directly, so a record missing `steps` or
    `discovery_path` is not a shape the runner ever produces."""
    base: JsonObject = {
        "id": fid,
        "passed": passed,
        "expected_tool": tool,
        "tokens": {"input": 10, "output": 2},
        "steps": 2,
        "discovery_path": "toolsets",
        "search_hit": None,
        "enabled_correct_toolset": passed,
        **over,
    }
    return cast(FixtureResult, cast(object, base))


def test_report_records_the_discovery_mode() -> None:
    """The `modes` wrapper outlives baseline's removal on purpose: eval/compare_runs
    and the committed result artifacts both index report["modes"]["discovery"]."""
    report = E.build_report("openai", "m", [fx("x", "p")], [result("a")], True, 6)

    assert set(report["modes"]) == {"discovery"}
    assert "discovery_summary" in report


def test_report_counts_passes_failures_and_skips_separately() -> None:
    discovery = [result("a"), result("b", passed=False), result("c", passed=False, skipped=True)]

    mode = E.build_report("openai", "m", [fx("x", "p")] * 3, discovery, True, 6)["modes"]["discovery"]

    assert (mode["passed"], mode["failed"], mode["skipped"]) == (1, 1, 1)


def test_report_attributes_by_tier() -> None:
    """by_tier drives the job summary's By-owner table, so a failure has to be
    attributed to the repository that owns the tool."""
    discovery = [result("c1"), result("d1", passed=False, tool="swag-dev-tools-load-skill")]

    report = E.build_report("openai", "m", [fx("x", "p")] * 2, discovery, True, 6)

    by_tier = report.get("by_tier") or {}
    assert by_tier["dev-tools"].get("failed_ids") == ["d1"]
    assert by_tier["core"]["passed"] == 1


def test_report_of_a_run_with_no_results_is_an_empty_table_not_a_crash() -> None:
    report = E.build_report("openai", "m", [fx("x", "p")], None, True, 6)

    assert report["modes"] == {} and report.get("by_tier") == {}


def test_report_is_json_serialisable() -> None:
    """It is written with json.dumps; a stray non-serialisable value would only
    surface at the very end of a paid run."""
    report = E.build_report("anthropic", "m", [fx("x", "p")], [result("a")], False, 8)

    assert json.loads(json.dumps(report))["system_prompt"] is False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------
def test_main_returns_one_on_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit code, not a traceback — and without reaching the network."""
    monkeypatch.setattr("sys.argv", ["eval.runner", "--modes", "nonsense"])

    assert E.main() == 1
    assert "unknown mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run_suite startup against the REAL fixture files
# ---------------------------------------------------------------------------
def test_startup_survives_the_real_fixtures_including_negatives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """The gap that let a KeyError reach CI.

    Every unit test built its own fixture dicts, so nothing exercised the
    startup path against the committed YAML. `run_suite` indexed
    `f["expected_tool"]` while computing which tools are absent from the
    catalogue — negative fixtures do not have one, so the whole run died after
    the catalogue probe, before a single fixture was graded, with no report and
    no partial output.
    """
    # setattr, not setenv: require_credentials reads module-level constants
    # captured at import, so setenv only works on a machine with a .env — which
    # is why this passed locally and failed in CI.
    for name in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(E, name, "test-value")
    monkeypatch.setattr(E, "build_client", const(object()))
    monkeypatch.setattr(E, "fetch_system_prompt", prompt_stub)
    # Deliberately a partial catalogue: the absent-tool report is the code under
    # test, so it has to actually have something to report.
    monkeypatch.setattr(E, "probe_catalogue", const({"shopware-entity-search"}))

    graded: list[Fixture] = []

    def fake_pass(
        _provider: str, _client: object, fixtures: list[Fixture], *_args: object, **_kwargs: object
    ) -> list[FixtureResult]:
        graded.extend(fixtures)
        return [E.skipped_result(f, "discovery") for f in fixtures]

    monkeypatch.setattr(E, "run_discovery_pass", fake_pass)

    code = E.run_suite(args(provider="openai", output=str(tmp_path / "r.json")))
    capsys.readouterr()

    assert code == 0
    negatives = [f for f in graded if f.get("expect_no_tool")]
    assert negatives, "the real admin fixtures must include negatives for this to be a regression test"


def test_startup_survives_the_real_store_fixtures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("SW_SC_ACCESS_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(E, name, "test-value")
    monkeypatch.setattr(E, "build_client", const(object()))
    monkeypatch.setattr(E, "fetch_system_prompt", prompt_stub)
    monkeypatch.setattr(E, "probe_catalogue", const(set[str]()))
    monkeypatch.setattr(E, "run_discovery_pass", const(list[FixtureResult]()))

    code = E.run_suite(args(provider="openai", endpoint="store", output=str(tmp_path / "s.json")))
    capsys.readouterr()

    assert code == 0


def test_a_crash_gets_its_own_exit_code_not_the_gates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash is not a verdict, and the advisory windows must not be able to
    downgrade it — a green job that actually crashed is worse than a red one.

    This is exactly what happened: a report-rendering bug exited 1, the
    re-baselining window read that as a threshold miss, and the run went green.
    """
    monkeypatch.setattr(E, "run_suite", raiser(RuntimeError("boom")))
    monkeypatch.setattr("sys.argv", ["runner", "--provider", "openai"])

    assert E.main() == E.CRASH_EXIT
    assert E.CRASH_EXIT != 1, "must be distinguishable from a gate failure"
    assert "crashed before producing a verdict" in capsys.readouterr().err


def test_a_config_error_still_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(E, "run_suite", raiser(E.ConfigError("nope")))
    monkeypatch.setattr("sys.argv", ["runner", "--provider", "openai"])

    assert E.main() == 1
    assert "nope" in capsys.readouterr().err
