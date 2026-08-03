"""Tool → owning-repository attribution.

The coverage checks are the important ones. Attribution is by name prefix, so
a server-side tool that matches no prefix would silently fall into
"unattributed" and its failures would go uncounted against any repo. These
fail the unit tests instead — same drift alarm as tests/test_fixtures.py.
"""

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

import ownership as OWN
from eval.result_schema import FixtureResult, ToolDef, as_list, as_object

ROOT = Path(__file__).resolve().parents[1]


def snapshot_tools(path: Path) -> list[ToolDef]:
    """The `tools` list out of a committed snapshot."""
    snap = as_object(cast(object, json.loads(path.read_text())))
    return [cast(ToolDef, cast(object, as_object(t))) for t in as_list(snap.get("tools"))]


def graded(*rows: tuple[str, str | None, bool]) -> list[FixtureResult]:
    """(id, expected_tool, passed) rows, as breakdown() takes them."""
    return [
        cast(FixtureResult, cast(object, {"id": fid, "expected_tool": tool, "passed": passed}))
        for fid, tool, passed in rows
    ]


def fixture_targets(*names: str) -> set[str]:
    """Every tool the named fixture files expect. Negative fixtures name none."""
    out: set[str] = set()
    for name in names:
        loaded = as_object(cast(object, yaml.safe_load((ROOT / "eval" / name).read_text())))
        for raw in as_list(loaded.get("fixtures")):
            if tool := as_object(raw).get("expected_tool"):
                out.add(str(tool))
    return out


SNAPSHOT_TOOLS = sorted(t["name"] for t in snapshot_tools(ROOT / "tool-history" / "latest.json"))
FIXTURE_TOOLS = sorted(fixture_targets("fixtures.yaml", "fixtures_store.yaml"))


@pytest.mark.parametrize("tool", SNAPSHOT_TOOLS)
def test_every_advertised_tool_has_an_owner(tool: str) -> None:
    assert OWN.owner_of(tool) != OWN.UNKNOWN, f"{tool} matches no prefix in OWNER_PREFIXES — add it"


@pytest.mark.parametrize("tool", FIXTURE_TOOLS)
def test_every_fixture_target_has_an_owner(tool: str) -> None:
    assert OWN.owner_of(tool) != OWN.UNKNOWN, f"{tool} matches no prefix in OWNER_PREFIXES — add it"


@pytest.mark.parametrize(
    "tool,owner",
    [
        ("shopware-entity-search", "core"),
        ("shopware-media-upload", "core"),
        ("shopware-tool-search", "core"),
        ("shopware-store-api-context", "core"),
        ("swag-dev-tools-log-search", "dev-tools"),
        ("merchant-order-summary", "merchant-tools"),
        ("shopware-ucp-cart-create", "agentic-commerce"),
    ],
)
def test_known_tools_map_to_their_repository(tool: str, owner: str) -> None:
    assert OWN.owner_of(tool) == owner


def test_ucp_beats_the_core_prefix() -> None:
    """shopware-ucp-* is agentic-commerce, not core — longest prefix must win.

    Order-dependent: move the bare 'shopware-' entry up in OWNER_PREFIXES and
    every UCP tool silently becomes core.
    """
    assert OWN.owner_of("shopware-ucp-checkout-complete") == "agentic-commerce"
    assert OWN.owner_of("shopware-store-api-context") == "core"


def test_unknown_prefix_is_flagged_not_guessed() -> None:
    assert OWN.owner_of("acme-something") == OWN.UNKNOWN


def test_discovery_tools_are_their_own_reporting_tier_but_still_core() -> None:
    for tool in OWN.DISCOVERY_TOOLS:
        assert OWN.tier_of(tool) == OWN.DISCOVERY
        # Gating rolls them into core: 9 fixtures is too small a denominator.
        assert OWN.owner_of(tool) == OWN.CORE


def test_breakdown_groups_and_rates_per_tier() -> None:
    results = graded(
        ("a", "shopware-entity-read", True),
        ("b", "shopware-entity-read", False),
        ("c", "swag-dev-tools-log-search", True),
        ("d", "shopware-tool-search", True),
    )

    b = OWN.breakdown(results)

    assert b["core"] == {"passed": 1, "total": 2, "ids": ["a", "b"], "failed_ids": ["b"], "rate": 0.5}
    assert b["dev-tools"]["rate"] == 1.0
    assert b[OWN.DISCOVERY]["total"] == 1


def test_breakdown_lists_every_graded_id_not_just_the_failures() -> None:
    """eval/summary.py merges these buckets across three suites, two of which
    grade the same fixture set. Without the full id list it can only add
    `total`, which double-counts those fixtures."""
    results = graded(
        ("a", "shopware-entity-read", True),
        ("b", "shopware-entity-read", False),
    )

    assert OWN.breakdown(results)["core"].get("ids") == ["a", "b"]


def test_breakdown_orders_most_critical_first() -> None:
    results = graded(
        ("m", "merchant-order-summary", True),
        ("d", "swag-dev-tools-scaffold", True),
        ("c", "shopware-entity-read", True),
        ("x", "shopware-tool-search", True),
    )

    assert list(OWN.breakdown(results)) == [OWN.DISCOVERY, "core", "dev-tools", "merchant-tools"]


def test_breakdown_keeps_unattributed_tiers_visible_at_the_end() -> None:
    results = graded(
        ("c", "shopware-entity-read", True),
        ("?", "acme-mystery", False),
    )

    b = OWN.breakdown(results)

    assert list(b)[-1] == OWN.UNKNOWN
    assert b[OWN.UNKNOWN].get("failed_ids") == ["?"]


def test_core_rate_includes_discovery_and_excludes_plugins() -> None:
    results = graded(
        ("a", "shopware-entity-read", True),
        ("b", "shopware-tool-search", False),
        ("c", "merchant-order-summary", False),
        ("d", "swag-dev-tools-scaffold", False),
    )

    assert OWN.core_rate(results) == (1, 2, 0.5)


def test_core_rate_is_vacuously_one_when_no_core_fixtures_ran() -> None:
    """The store suite is almost entirely UCP; it must not fail a core gate it
    has no fixtures for."""
    assert OWN.core_rate(graded(("u", "shopware-ucp-cart-get", False))) == (0, 0, 1.0)


def test_core_rate_handles_empty() -> None:
    assert OWN.core_rate([]) == (0, 0, 1.0)
    assert OWN.breakdown([]) == {}


def test_the_current_fixture_set_splits_across_all_four_repos() -> None:
    """Guards the premise: if attribution ever collapses everything into one
    bucket, the per-owner table becomes decoration."""
    owners = {OWN.owner_of(t) for t in FIXTURE_TOOLS}

    assert owners == {"core", "dev-tools", "merchant-tools", "agentic-commerce"}


def test_negative_fixtures_are_not_attributed_to_any_repository() -> None:
    """They name no tool, so no repo owns them. Attributing would file every
    one under "unattributed" and invent a tier that reads like a drift alarm."""
    results = graded(
        ("a", "shopware-entity-search", True),
        ("n", None, False),
    )

    tiers = OWN.breakdown(results)

    assert OWN.UNKNOWN not in tiers
    assert tiers["core"]["total"] == 1


def test_negative_fixtures_stay_out_of_the_core_denominator() -> None:
    """They still count in the overall rate — that is the axis they belong on."""
    results = graded(
        ("a", "shopware-entity-search", True),
        ("n", None, False),
    )

    assert OWN.core_rate(results) == (1, 1, 1.0)


# ---------------------------------------------------------------------------
# Negative fixtures carry `expected_tool: None`
# ---------------------------------------------------------------------------
def test_a_missing_expected_tool_is_unknown_not_a_crash() -> None:
    """`result.get("expected_tool", "")` returns None, not "", because the key is
    present with a null value. That crashed a full CI run at the gate, after the
    LLM pass had already been paid for."""
    assert OWN.owner_of(None) == OWN.UNKNOWN
    assert OWN.tier_of(None) == OWN.UNKNOWN
    assert OWN.owner_of("") == OWN.UNKNOWN


def test_the_real_negative_fixtures_survive_the_gate_path() -> None:
    """Over the actual fixture files, not a hand-built dict — the previous two
    fixes for this same shape passed their unit tests and still broke CI."""

    import yaml

    for name in ("fixtures.yaml", "fixtures_store.yaml"):
        loaded = as_object(cast(object, yaml.safe_load((ROOT / "eval" / name).read_text())))
        for raw in as_list(loaded.get("fixtures")):
            # exactly what print_gate does
            tool = str(as_object(raw).get("expected_tool", ""))
            assert OWN.owner_of(tool) in OWN.TIER_ORDER
            assert OWN.tier_of(tool) in OWN.TIER_ORDER
