"""Execution safety classification.

The coverage checks are the important ones, same as tests/test_ownership.py: a
tool the server grows must be classified deliberately rather than falling into a
default. The default here is "do not execute", so the failure mode is a silently
weaker eval rather than a deleted entity — but it is still a failure, and it
should surface in a diff.
"""

import json
from pathlib import Path

import pytest

import toolclass as TC

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((ROOT / "tool-history" / "latest.json").read_text())
SNAPSHOT_TOOLS = sorted(t["name"] for t in SNAPSHOT["tools"])
SCHEMA_DRY_RUN = sorted(
    t["name"] for t in SNAPSHOT["tools"] if TC.DRY_RUN_KEY in ((t.get("inputSchema") or {}).get("properties") or {})
)


# ---------------------------------------------------------------------------
# Coverage against the committed snapshot
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", SNAPSHOT_TOOLS)
def test_every_advertised_tool_is_classified(tool):
    assert TC.classify(tool) is not None, (
        f"{tool} is in the catalogue but in no class in toolclass.py — "
        "decide whether it is read-only, dry-runnable or unsafe"
    )


def test_the_dry_runnable_set_matches_what_the_schemas_declare():
    """The server is the authority on which tools have a safe path.

    Drifting from the schemas in either direction is a real bug: a tool listed
    here without a dryRun property would be called with an argument it rejects,
    and one omitted would be executed for real.
    """
    assert sorted(TC.DRY_RUNNABLE) == SCHEMA_DRY_RUN


def test_no_tool_is_in_two_classes():
    assert not (TC.READ_ONLY & TC.DRY_RUNNABLE)
    assert not (TC.READ_ONLY & TC.UNSAFE)
    assert not (TC.DRY_RUNNABLE & TC.UNSAFE)


def test_the_admin_catalogue_is_fully_covered_with_no_stale_entries():
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


def test_every_store_fixture_target_is_classified():
    """The gap this catches: the Store suite ran entirely on tools no class
    covered, so every one of its fixtures silently degraded to selection-only
    grading — the thing execution was added to stop."""
    import yaml

    store = yaml.safe_load((ROOT / "eval" / "fixtures_store.yaml").read_text())["fixtures"]
    targets = {f["expected_tool"] for f in store if f.get("expected_tool")}
    unclassified = sorted(t for t in targets if TC.classify(t) is None)

    assert not unclassified, f"Store fixture targets with no class: {unclassified}"


def test_store_mutations_are_unsafe_until_the_endpoint_is_snapshotted():
    """Conservative by necessity: with no schema to read a dryRun out of,
    guessing the other way would place real orders."""
    for tool in ("shopware-ucp-checkout-complete", "shopware-ucp-cart-create"):
        assert TC.classify(tool) == "unsafe"
        assert TC.is_executable(tool) is False


def test_unsafe_tools_have_no_dry_run_to_hide_behind():
    """What makes them unsafe. If one grows a dryRun property it should move to
    DRY_RUNNABLE and start participating in result assertions."""
    assert not (TC.UNSAFE & set(SCHEMA_DRY_RUN))


# ---------------------------------------------------------------------------
# The execution boundary
# ---------------------------------------------------------------------------
def test_read_only_and_dry_runnable_are_executable():
    assert TC.is_executable("shopware-entity-search") is True
    assert TC.is_executable("shopware-entity-delete") is True


def test_unsafe_and_unknown_tools_are_not_executed():
    """Unknown defaults to no. A tool the server grew since the last snapshot
    has unknown blast radius, and 'probably fine' is how an eval deletes
    something."""
    assert TC.is_executable("shopware-media-upload") is False
    assert TC.is_executable("some-tool-shipped-yesterday") is False
    assert TC.classify("some-tool-shipped-yesterday") is None


# ---------------------------------------------------------------------------
# prepare_call
# ---------------------------------------------------------------------------
def test_a_mutating_call_gets_dry_run_forced_on():
    args, forced = TC.prepare_call("shopware-entity-delete", {"entity": "product", "ids": "[]"})

    assert args["dryRun"] is True
    assert forced is True
    assert args["entity"] == "product", "the model's other arguments are untouched"


def test_a_model_asking_for_a_real_delete_is_overridden():
    """The eval's safety cannot depend on the thing under test agreeing to it."""
    args, forced = TC.prepare_call("shopware-entity-delete", {"dryRun": False})

    assert args["dryRun"] is True
    assert forced is True


def test_a_model_that_already_asked_for_a_dry_run_is_not_recorded_as_overridden():
    args, forced = TC.prepare_call("shopware-entity-delete", {"dryRun": True})

    assert args["dryRun"] is True
    assert forced is False, "nothing was overridden, so nothing should be reported as overridden"


def test_a_read_only_call_is_passed_through_untouched():
    args, forced = TC.prepare_call("shopware-entity-search", {"entity": "product"})

    assert args == {"entity": "product"}
    assert forced is False
    assert TC.DRY_RUN_KEY not in args, "adding dryRun to a tool without it would be a schema error"


def test_prepare_call_does_not_mutate_the_callers_arguments():
    """The original is what gets recorded as `selected_input` — the graded
    artefact must be what the model actually said, not what we rewrote."""
    original = {"entity": "product"}

    TC.prepare_call("shopware-entity-delete", original)

    assert original == {"entity": "product"}


def test_prepare_call_tolerates_no_arguments():
    assert TC.prepare_call("shopware-entity-search", None) == ({}, False)
    assert TC.prepare_call("shopware-entity-delete", None) == ({"dryRun": True}, True)


def test_an_unclassified_tool_is_not_given_a_dry_run():
    """It is not executed at all, so silently adding an argument it may not
    accept would only muddy the record."""
    args, forced = TC.prepare_call("unknown-tool", {"a": 1})

    assert args == {"a": 1} and forced is False
