"""Execution safety classification.

The coverage checks are the important ones, same as tests/test_ownership.py: a
tool the server grows must be classified deliberately rather than falling into a
default. The default here is "do not execute", so the failure mode is a silently
weaker eval rather than a deleted entity — but it is still a failure, and it
should surface in a diff.
"""

import json
from pathlib import Path
from typing import cast

import pytest

import toolclass as TC
from eval.result_schema import JsonObject, ToolDef, as_list, as_object

ROOT = Path(__file__).resolve().parents[1]


def snapshot_tools(path: Path) -> list[ToolDef]:
    """The `tools` list out of a committed snapshot."""
    snap = as_object(cast(object, json.loads(path.read_text())))
    return [cast(ToolDef, cast(object, as_object(t))) for t in as_list(snap.get("tools"))]


TOOLS = snapshot_tools(ROOT / "tool-history" / "latest.json")
SNAPSHOT_TOOLS = sorted(t["name"] for t in TOOLS)
SCHEMA_DRY_RUN = sorted(
    t["name"] for t in TOOLS if TC.DRY_RUN_KEY in as_object(as_object(t.get("inputSchema")).get("properties"))
)


# ---------------------------------------------------------------------------
# Coverage against the committed snapshot
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", SNAPSHOT_TOOLS)
def test_every_advertised_tool_is_classified(tool: str) -> None:
    assert TC.classify(tool) is not None, (
        f"{tool} is in the catalogue but in no class in toolclass.py — "
        "decide whether it is read-only, dry-runnable or unsafe"
    )


def test_the_dry_runnable_set_matches_what_the_schemas_declare() -> None:
    """The server is the authority on which tools have a safe path.

    Drifting from the schemas in either direction is a real bug: a tool listed
    here without a dryRun property would be called with an argument it rejects,
    and one omitted would be executed for real.
    """
    # Admin-scoped: the Store tools are in DRY_RUNNABLE too, and the admin
    # snapshot knows nothing about them. test_store_tools_that_declare_dry_run_
    # are_not_guessed_unsafe covers those once store.json lands.
    admin = sorted(t for t in TC.DRY_RUNNABLE if not t.startswith(("shopware-ucp-", "shopware-store-api-")))
    assert admin == SCHEMA_DRY_RUN


def test_no_tool_is_in_two_classes() -> None:
    assert not (TC.READ_ONLY & TC.DRY_RUNNABLE)
    assert not (TC.READ_ONLY & TC.UNSAFE)
    assert not (TC.DRY_RUNNABLE & TC.UNSAFE)


def test_the_admin_catalogue_is_fully_covered_with_no_stale_entries() -> None:
    """Every admin tool classified, and nothing classified that admin dropped.

    Not equality: the Store endpoint has no committed snapshot, so its
    `shopware-ucp-*` tools are classified here without one to check against.
    They are asserted separately below.
    """
    classified = TC.all_classified()
    assert not set(SNAPSHOT_TOOLS) - classified, "admin tools with no class"
    store_prefixes = ("shopware-ucp-", "shopware-store-api-")
    stale = {t for t in classified - set(SNAPSHOT_TOOLS) if not t.startswith(store_prefixes)}
    assert not stale, f"classified but no longer in the admin catalogue: {sorted(stale)}"


def test_every_store_fixture_target_is_classified() -> None:
    """The gap this catches: the Store suite ran entirely on tools no class
    covered, so every one of its fixtures silently degraded to selection-only
    grading — the thing execution was added to stop."""
    import yaml

    loaded = as_object(cast(object, yaml.safe_load((ROOT / "eval" / "fixtures_store.yaml").read_text())))
    targets = {str(t) for raw in as_list(loaded.get("fixtures")) if (t := as_object(raw).get("expected_tool"))}
    unclassified = sorted(t for t in targets if TC.classify(t) is None)

    assert not unclassified, f"Store fixture targets with no class: {unclassified}"


def test_store_mutations_are_dry_runnable_now_that_the_plugin_declares_it() -> None:
    """These were guessed unsafe while there was no Store schema to read. The
    plugin added dryRun to exactly its mutating tools, so they are executable
    again — checkout-complete is the one that can take money, and it is only
    callable at all because the server offers a safe path."""
    for tool in ("shopware-ucp-checkout-complete", "shopware-ucp-cart-create"):
        assert TC.classify(tool) == "dry_runnable"
        args, forced = TC.prepare_call(tool, {})
        assert args["dryRun"] is True and forced is True


def test_unsafe_tools_have_no_dry_run_to_hide_behind() -> None:
    """What makes them unsafe. If one grows a dryRun property it should move to
    DRY_RUNNABLE and start participating in result assertions."""
    assert not (TC.UNSAFE & set(SCHEMA_DRY_RUN))


# ---------------------------------------------------------------------------
# The execution boundary
# ---------------------------------------------------------------------------
def test_read_only_and_dry_runnable_are_executable() -> None:
    assert TC.is_executable("shopware-entity-search") is True
    assert TC.is_executable("shopware-entity-delete") is True


def test_unsafe_and_unknown_tools_are_not_executed() -> None:
    """Unknown defaults to no. A tool the server grew since the last snapshot
    has unknown blast radius, and 'probably fine' is how an eval deletes
    something."""
    assert TC.is_executable("shopware-media-upload") is False
    assert TC.is_executable("some-tool-shipped-yesterday") is False
    assert TC.classify("some-tool-shipped-yesterday") is None


# ---------------------------------------------------------------------------
# prepare_call
# ---------------------------------------------------------------------------
def test_a_mutating_call_gets_dry_run_forced_on() -> None:
    args, forced = TC.prepare_call("shopware-entity-delete", {"entity": "product", "ids": "[]"})

    assert args["dryRun"] is True
    assert forced is True
    assert args["entity"] == "product", "the model's other arguments are untouched"


def test_a_model_asking_for_a_real_delete_is_overridden() -> None:
    """The eval's safety cannot depend on the thing under test agreeing to it."""
    args, forced = TC.prepare_call("shopware-entity-delete", {"dryRun": False})

    assert args["dryRun"] is True
    assert forced is True


def test_a_model_that_already_asked_for_a_dry_run_is_not_recorded_as_overridden() -> None:
    args, forced = TC.prepare_call("shopware-entity-delete", {"dryRun": True})

    assert args["dryRun"] is True
    assert forced is False, "nothing was overridden, so nothing should be reported as overridden"


def test_a_read_only_call_is_passed_through_untouched() -> None:
    args, forced = TC.prepare_call("shopware-entity-search", {"entity": "product"})

    assert args == {"entity": "product"}
    assert forced is False
    assert TC.DRY_RUN_KEY not in args, "adding dryRun to a tool without it would be a schema error"


def test_prepare_call_does_not_mutate_the_callers_arguments() -> None:
    """The original is what gets recorded as `selected_input` — the graded
    artefact must be what the model actually said, not what we rewrote."""
    original: JsonObject = {"entity": "product"}

    TC.prepare_call("shopware-entity-delete", original)

    assert original == {"entity": "product"}


def test_prepare_call_tolerates_no_arguments() -> None:
    assert TC.prepare_call("shopware-entity-search", None) == ({}, False)
    assert TC.prepare_call("shopware-entity-delete", None) == ({"dryRun": True}, True)


def test_an_unclassified_tool_is_not_given_a_dry_run() -> None:
    """It is not executed at all, so silently adding an argument it may not
    accept would only muddy the record."""
    args, forced = TC.prepare_call("unknown-tool", {"a": 1})

    assert args == {"a": 1} and forced is False


# ---------------------------------------------------------------------------
# Store classification, once the Store catalogue has been snapshotted
# ---------------------------------------------------------------------------
# Every shopware-ucp-* tool is currently classified by hand from its name,
# because there has never been a Store snapshot to read a `dryRun` property out
# of. Anything that might mutate was therefore called unsafe, which is why the
# whole Store suite is graded on selection alone.
#
# This is inert until the snapshot lands, then it fails for any Store tool whose
# schema disagrees with the guess — including any that turn out to have a dryRun
# and should move to DRY_RUNNABLE and start being executed for real.
STORE_SNAPSHOT = ROOT / "tool-history" / "store.json"
store_snapshot_required = pytest.mark.skipif(
    not STORE_SNAPSHOT.exists(),
    reason="tool-history/store.json not committed yet — the nightly reconciliation PR adds it",
)


@store_snapshot_required
def test_every_store_tool_is_classified() -> None:
    tools = [t["name"] for t in snapshot_tools(STORE_SNAPSHOT)]
    unclassified = sorted(t for t in tools if TC.classify(t) is None)

    assert not unclassified, f"Store tools with no class: {unclassified}"


@store_snapshot_required
def test_store_tools_that_declare_dry_run_are_not_guessed_unsafe() -> None:
    """A Store tool with a dryRun can be executed safely, so leaving it unsafe
    costs real signal — result assertions and recovery both switch off for it."""
    declares = {
        t["name"]
        for t in snapshot_tools(STORE_SNAPSHOT)
        if TC.DRY_RUN_KEY in as_object(as_object(t.get("inputSchema")).get("properties"))
    }
    misfiled = sorted(declares & TC.UNSAFE)

    assert not misfiled, f"these declare dryRun and should move to DRY_RUNNABLE: {misfiled}"


# ---------------------------------------------------------------------------
# The agentic-commerce plugin is isolated in ucp.py
# ---------------------------------------------------------------------------
def test_ucp_tools_are_classified_in_their_own_module() -> None:
    """The plugin is optional and may go away. Its specifics live in one file so
    removing it is deleting that file, not picking entries out of three sets."""
    import ucp

    assert ucp.all_classified(), "ucp.py must own the plugin's classification"
    assert ucp.all_classified() <= TC.all_classified(), "and toolclass must merge it in"
    assert all(t.startswith("shopware-ucp-") for t in ucp.all_classified())


def test_toolclass_carries_no_ucp_entries_of_its_own() -> None:
    """The regression this split prevents: a UCP tool added straight into
    toolclass would survive deleting ucp.py and quietly keep being executed."""
    import ucp

    for name, own in (("READ_ONLY", TC.READ_ONLY), ("DRY_RUNNABLE", TC.DRY_RUNNABLE), ("UNSAFE", TC.UNSAFE)):
        strays = {t for t in own if t.startswith("shopware-ucp-")} - ucp.all_classified()
        assert not strays, f"{name} has UCP tools that ucp.py does not own: {sorted(strays)}"


def test_store_api_context_is_core_and_stays_behind() -> None:
    """It rides the Store endpoint but is Shopware core, so dropping the plugin
    must not take it with them."""
    import ucp

    assert "shopware-store-api-context" not in ucp.all_classified()
    assert TC.classify("shopware-store-api-context") == "read_only"


def test_the_agent_header_carries_a_quoted_profile_uri() -> None:
    """The SDK reads it with /profile="([^"]+)"/ — an unquoted or absent URI is
    rejected before the tool runs."""
    import re

    import ucp

    header = ucp.agent_header("http://shop.example.com/")

    profile = re.search(r'profile="([^"]+)"', header)
    assert profile is not None
    assert profile.group(1) == "http://shop.example.com/.well-known/ucp"


def test_an_explicit_profile_uri_wins() -> None:
    import ucp

    assert 'profile="https://agent.example/p"' in ucp.agent_header("http://shop.test", "https://agent.example/p")


def test_mutating_ucp_calls_carry_an_idempotency_key() -> None:
    """Without it every dry run fails on "Idempotency key is required for
    mutating UCP requests" before the tool does any work, which reads as a
    tool-quality failure in the results and is not one."""
    import ucp

    for tool in sorted(ucp.DRY_RUNNABLE | ucp.UNSAFE):
        assert ucp.call_headers(tool).get("Idempotency-Key"), tool


def test_reads_and_non_ucp_tools_get_no_extra_headers() -> None:
    """The key identifies a mutation. Sending one on a read is noise, and sending
    one on an admin tool would leak plugin specifics onto the other endpoint."""
    import ucp

    for tool in sorted(ucp.READ_ONLY) + ["shopware-entity-search", "shopware-store-api-context"]:
        assert ucp.call_headers(tool) == {}, tool


def test_each_call_gets_a_fresh_key() -> None:
    """The server replays a completed response for a repeated key, so a shared
    one would serve the previous fixture's answer to the next."""
    import ucp

    keys = {ucp.call_headers("shopware-ucp-cart-create")["Idempotency-Key"] for _ in range(20)}
    assert len(keys) == 20
