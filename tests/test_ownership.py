"""Tool → owning-repository attribution.

The coverage checks are the important ones. Attribution is by name prefix, so
a server-side tool that matches no prefix would silently fall into
"unattributed" and its failures would go uncounted against any repo. These
fail the unit tests instead — same drift alarm as tests/test_fixtures.py.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("eval_ownership", ROOT / "ownership.py")
OWN = importlib.util.module_from_spec(_spec)
sys.modules["eval_ownership"] = OWN
_spec.loader.exec_module(OWN)

SNAPSHOT_TOOLS = sorted(t["name"] for t in json.loads((ROOT / "tool-history" / "latest.json").read_text())["tools"])
FIXTURE_TOOLS = sorted(
    {
        f["expected_tool"]
        for name in ("fixtures.yaml", "fixtures_store.yaml")
        for f in yaml.safe_load((ROOT / "eval" / name).read_text())["fixtures"]
    }
)


@pytest.mark.parametrize("tool", SNAPSHOT_TOOLS)
def test_every_advertised_tool_has_an_owner(tool):
    assert OWN.owner_of(tool) != OWN.UNKNOWN, f"{tool} matches no prefix in OWNER_PREFIXES — add it"


@pytest.mark.parametrize("tool", FIXTURE_TOOLS)
def test_every_fixture_target_has_an_owner(tool):
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
def test_known_tools_map_to_their_repository(tool, owner):
    assert OWN.owner_of(tool) == owner


def test_ucp_beats_the_core_prefix():
    """shopware-ucp-* is agentic-commerce, not core — longest prefix must win.

    Order-dependent: move the bare 'shopware-' entry up in OWNER_PREFIXES and
    every UCP tool silently becomes core.
    """
    assert OWN.owner_of("shopware-ucp-checkout-complete") == "agentic-commerce"
    assert OWN.owner_of("shopware-store-api-context") == "core"


def test_unknown_prefix_is_flagged_not_guessed():
    assert OWN.owner_of("acme-something") == OWN.UNKNOWN


def test_discovery_tools_are_their_own_reporting_tier_but_still_core():
    for tool in OWN.DISCOVERY_TOOLS:
        assert OWN.tier_of(tool) == OWN.DISCOVERY
        # Gating rolls them into core: 9 fixtures is too small a denominator.
        assert OWN.owner_of(tool) == OWN.CORE


def test_breakdown_groups_and_rates_per_tier():
    results = [
        {"id": "a", "expected_tool": "shopware-entity-read", "passed": True},
        {"id": "b", "expected_tool": "shopware-entity-read", "passed": False},
        {"id": "c", "expected_tool": "swag-dev-tools-log-search", "passed": True},
        {"id": "d", "expected_tool": "shopware-tool-search", "passed": True},
    ]

    b = OWN.breakdown(results)

    assert b["core"] == {"passed": 1, "total": 2, "failed_ids": ["b"], "rate": 0.5}
    assert b["dev-tools"]["rate"] == 1.0
    assert b[OWN.DISCOVERY]["total"] == 1


def test_breakdown_orders_most_critical_first():
    results = [
        {"id": "m", "expected_tool": "merchant-order-summary", "passed": True},
        {"id": "d", "expected_tool": "swag-dev-tools-scaffold", "passed": True},
        {"id": "c", "expected_tool": "shopware-entity-read", "passed": True},
        {"id": "x", "expected_tool": "shopware-tool-search", "passed": True},
    ]

    assert list(OWN.breakdown(results)) == [OWN.DISCOVERY, "core", "dev-tools", "merchant-tools"]


def test_breakdown_keeps_unattributed_tiers_visible_at_the_end():
    results = [
        {"id": "c", "expected_tool": "shopware-entity-read", "passed": True},
        {"id": "?", "expected_tool": "acme-mystery", "passed": False},
    ]

    b = OWN.breakdown(results)

    assert list(b)[-1] == OWN.UNKNOWN
    assert b[OWN.UNKNOWN]["failed_ids"] == ["?"]


def test_core_rate_includes_discovery_and_excludes_plugins():
    results = [
        {"id": "a", "expected_tool": "shopware-entity-read", "passed": True},
        {"id": "b", "expected_tool": "shopware-tool-search", "passed": False},
        {"id": "c", "expected_tool": "merchant-order-summary", "passed": False},
        {"id": "d", "expected_tool": "swag-dev-tools-scaffold", "passed": False},
    ]

    assert OWN.core_rate(results) == (1, 2, 0.5)


def test_core_rate_is_vacuously_one_when_no_core_fixtures_ran():
    """The store suite is almost entirely UCP; it must not fail a core gate it
    has no fixtures for."""
    assert OWN.core_rate([{"id": "u", "expected_tool": "shopware-ucp-cart-get", "passed": False}]) == (0, 0, 1.0)


def test_core_rate_handles_empty():
    assert OWN.core_rate([]) == (0, 0, 1.0)
    assert OWN.breakdown([]) == {}


def test_the_current_fixture_set_splits_across_all_four_repos():
    """Guards the premise: if attribution ever collapses everything into one
    bucket, the per-owner table becomes decoration."""
    owners = {OWN.owner_of(t) for t in FIXTURE_TOOLS}

    assert owners == {"core", "dev-tools", "merchant-tools", "agentic-commerce"}
