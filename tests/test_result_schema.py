"""The per-fixture result contract: DiscoveryState.to_result and schema_version.

The record shape is read by five modules (see eval/result_schema). These test it
at the source — assembled from a DiscoveryState without driving a live loop, and
present with a version on every producer — so a dropped or renamed key fails here
rather than as a KeyError mid-report.
"""

from eval import runner as E
from eval.result_schema import SCHEMA_VERSION, FixtureResult


def _fixture(**over):
    return {"id": "f1", "prompt": "p", "expected_tool": "shopware-entity-read", "category": "unambiguous", **over}


def test_to_result_carries_the_schema_version_and_every_declared_key():
    st = E.DiscoveryState(arm="discovery")
    result = st.to_result(_fixture(), passed=False, latency=1.2)

    assert result["schema_version"] == SCHEMA_VERSION
    # Every key the record emits must be declared in the shared TypedDict, so a
    # consumer typed against it cannot read a key no producer writes.
    declared = set(FixtureResult.__annotations__)
    assert set(result) <= declared
    # And the graded producer emits the full contract, not a subset. Excluded:
    # the non-graded producers' keys, and `attempts`/`_line`, which the discovery
    # worker attaches AFTER to_result rather than to_result itself.
    graded_only = declared - {"skipped", "skip_reason", "error", "attempts", "_line"}
    assert graded_only <= set(result)


def test_discovery_path_is_derived_from_the_meta_calls_made():
    st = E.DiscoveryState(arm="discovery")
    st.selected_tool = "shopware-entity-read"
    st.meta_calls = [{"tool": "shopware-tool-search"}, {"tool": "shopware-toolset-enable"}]

    assert st.to_result(_fixture(), passed=True, latency=0.0)["discovery_path"] == "mixed"


def test_no_answer_means_no_discovery_path():
    st = E.DiscoveryState(arm="discovery")  # selected_tool stays None

    assert st.to_result(_fixture(), passed=False, latency=0.0)["discovery_path"] == "none"


def test_fail_reason_is_cleared_on_a_pass():
    st = E.DiscoveryState(arm="discovery", fail_reason="no_tool_call")

    assert st.to_result(_fixture(), passed=True, latency=0.0)["fail_reason"] is None


def test_first_try_needs_both_the_right_tool_and_a_working_call():
    st = E.DiscoveryState(arm="discovery")
    st.attempted_tools = [{"tool": "t", "correct": True, "ok": False}]

    # Right tool, but the call did not satisfy the assertion — not a first-try win.
    assert st.to_result(_fixture(), passed=False, latency=0.0)["first_try"] is False


def test_enabled_correct_toolset_tracks_what_was_enabled():
    st = E.DiscoveryState(arm="discovery", enabled_toolsets=["entity"])
    result = st.to_result(_fixture(expected_toolset="entity"), passed=True, latency=0.0)

    assert result["enabled_correct_toolset"] is True


def test_skipped_and_error_results_are_versioned_too():
    """The back-compat branches in summary.py key off the version, so every
    producer has to stamp it — not just the graded path."""
    assert E.skipped_result(_fixture(), "discovery")["schema_version"] == SCHEMA_VERSION
    assert E.error_result(_fixture(), "discovery", ValueError("boom"))["schema_version"] == SCHEMA_VERSION
