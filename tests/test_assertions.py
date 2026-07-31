"""Result assertions.

The load-bearing distinction is between a call the server rejected and one it
ran and returned nothing for. Getting that wrong in one direction turns the
suite into a test of the demo-data seed; in the other it lets malformed calls
pass as long as the tool name was right.
"""

import json

import pytest

from eval import assertions as A


def payload(**data):
    return json.dumps(data)


# ---------------------------------------------------------------------------
# The tiers
# ---------------------------------------------------------------------------
def test_the_none_tier_passes_whatever_happened():
    """Selection-only, for tools that cannot be executed at all."""
    assert A.check("none", None, "everything is on fire") == (True, None)


def test_the_accepted_tier_passes_on_an_empty_result():
    """The honest tier for a fixture that has to invent an id: the model cannot
    know a real UUID, so 'not found' says the call was well-formed."""
    assert A.check("accepted", payload(data=[]), None) == (True, None)


def test_the_accepted_tier_still_fails_a_rejected_call():
    passed, reason = A.check("accepted", None, "Validation failed: entity is required")

    assert passed is False and reason == "invalid_arguments"


def test_the_data_tier_wants_something_back():
    assert A.check({"tier": "data", "min_items": {"path": "data", "n": 1}}, payload(data=[{"id": "x"}]), None) == (
        True,
        None,
    )

    passed, reason = A.check({"tier": "data", "min_items": {"path": "data", "n": 1}}, payload(data=[]), None)
    assert passed is False and reason == "too_few:data<1"


# ---------------------------------------------------------------------------
# Whose fault the failure is
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        "Validation failed for parameter 'entity'",
        "Invalid filter syntax",
        "Missing required parameter: ids",
        "Unknown parameter 'limits'",
        "Value must be an integer",
        "Request body malformed",
    ],
)
def test_a_rejected_call_is_the_models_fault(error):
    assert A.is_validation_error(error) is True
    assert A.check("accepted", None, error) == (False, "invalid_arguments")


@pytest.mark.parametrize(
    "error",
    [
        "500 Internal Server Error",
        "Connection reset by peer",
        "Plugin SwagMcpDevTools is not installed",
        "429 Too Many Requests",
    ],
)
def test_an_environment_failure_is_reported_separately(error):
    """A 500 or a missing plugin is not evidence about a tool description, so it
    must be distinguishable upstream rather than counted as a wrong answer."""
    assert A.is_validation_error(error) is False
    assert A.check("accepted", None, error) == (False, "tool_error")


def test_no_error_at_all_is_not_a_validation_error():
    assert A.is_validation_error(None) is False
    assert A.is_validation_error("") is False


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
def test_has_keys_walks_a_dotted_path():
    spec = {"tier": "data", "has_keys": ["data.total"]}

    assert A.check(spec, payload(data={"total": 7}), None) == (True, None)
    assert A.check(spec, payload(data={}), None) == (False, "missing:data.total")


def test_a_key_present_but_null_is_missing():
    """A tool that answers `{"total": null}` has not supplied the total."""
    assert A.check({"tier": "data", "has_keys": ["total"]}, payload(total=None), None) == (False, "missing:total")


def test_min_items_counts_lists_dicts_and_strings():
    spec = {"tier": "data", "min_items": {"path": "data", "n": 2}}

    assert A.check(spec, payload(data=[1, 2]), None)[0] is True
    assert A.check(spec, payload(data={"a": 1, "b": 2}), None)[0] is True
    assert A.check(spec, payload(data="ab"), None)[0] is True
    assert A.check(spec, payload(data=[1]), None) == (False, "too_few:data<2")


def test_min_items_against_a_scalar_is_reported_as_a_shape_problem():
    spec = {"tier": "data", "min_items": {"path": "data", "n": 1}}
    assert A.check(spec, payload(data=7), None) == (False, "not_a_collection:data")


def test_min_items_on_an_absent_path():
    spec = {"tier": "data", "min_items": {"path": "results", "n": 1}}
    assert A.check(spec, payload(data=[1]), None) == (False, "not_a_collection:results")


def test_contains_matches_the_raw_response_text():
    spec = {"tier": "data", "contains": ["productNumber"]}

    assert A.check(spec, payload(data=[{"productNumber": "SW-1"}]), None) == (True, None)
    assert A.check(spec, payload(data=[{"id": "x"}]), None) == (False, "missing_text:productNumber")


def test_predicates_are_all_required_and_the_first_failure_is_reported():
    spec = {"tier": "data", "has_keys": ["data"], "min_items": {"path": "data", "n": 5}}
    assert A.check(spec, payload(data=[1]), None) == (False, "too_few:data<5")


def test_an_unparseable_body_fails_the_data_tier_but_not_the_accepted_one():
    """Accepted only claims the server took the call; data claims it answered."""
    assert A.check("data", "<html>gateway timeout</html>", None) == (False, "unreadable_result")
    assert A.check("accepted", "<html>gateway timeout</html>", None) == (True, None)


def test_an_empty_body_is_unreadable_for_the_data_tier():
    assert A.check("data", "", None) == (False, "unreadable_result")


# ---------------------------------------------------------------------------
# normalise
# ---------------------------------------------------------------------------
def test_a_fixture_with_no_expectation_gets_the_honest_default():
    """`accepted` is what a fixture with nothing declared actually supports."""
    assert A.normalise(None) == {"tier": "accepted"}
    assert A.check(None, payload(data=[]), None) == (True, None)


def test_normalise_accepts_a_bare_tier_or_a_mapping():
    assert A.normalise("data") == {"tier": "data"}
    assert A.normalise({"tier": "data", "contains": ["x"]}) == {"tier": "data", "contains": ["x"]}


def test_every_declared_tier_is_understood():
    for tier in A.TIERS:
        passed, _ = A.check(tier, payload(data=[{"id": 1}]), None)
        assert passed is True


# ---------------------------------------------------------------------------
# not-found vs malformed — the distinction the `accepted` tier exists for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        "Checkout with id co_xyz789 not found",
        "No such cart",
        "Entity does not exist",
        "Could not be found",
        "404 Not Found",
    ],
)
def test_an_id_that_does_not_resolve_is_accepted(error):
    """The whole point of the tier. A fixture cannot know a real UUID, so
    "not found" proves the call was well-formed — which is what is under test."""
    assert A.is_not_found(error) is True
    assert A.check("accepted", None, error) == (True, None)


def test_the_data_tier_still_fails_on_not_found():
    """`data` claims the call returned something; not-found is not something."""
    assert A.check("data", None, "Checkout not found") == (False, "not_found")


def test_a_malformed_request_fails_even_at_the_accepted_tier():
    """Not-found is the fixture's invented value; a missing parameter is the
    model's own doing and must still fail."""
    assert A.check("accepted", None, "Missing required parameter: checkoutId") == (False, "invalid_arguments")


def test_not_found_wins_over_the_word_invalid():
    """ "invalid" turns up inside plenty of not-found messages. Of the two ways
    to be wrong, wrongly failing a fixture is worse — it sends someone off to
    rewrite a description that was fine."""
    assert A.check("accepted", None, "Invalid or unknown checkout id co_xyz789 — not found") == (True, None)


def test_the_store_regression_this_fixes():
    """15 of 15 executed Store fixtures failed this way while all 25 unexecuted
    ones passed, and it read as a description problem for three runs.

    The model named the right tool, called it with the id the prompt gave it,
    and the server said that checkout does not exist.
    """
    passed, reason = A.check("accepted", None, "Checkout session co_xyz789 not found")

    assert passed is True and reason is None


def test_an_environment_failure_is_still_neither():
    assert A.check("accepted", None, "500 Internal Server Error") == (False, "tool_error")
    assert A.is_not_found("500 Internal Server Error") is False


# ---------------------------------------------------------------------------
# In-band failures — HTTP 200 with success: false
# ---------------------------------------------------------------------------
def test_a_success_false_body_is_a_failure_not_a_pass():
    """UCP answers a rejected request with HTTP 200 and success: false, so there
    is no transport error and the call looks clean.

    Without this the `accepted` tier passed every one: the Store suite would
    have gone green while not a single tool did anything — worse than the
    failure it replaced.
    """
    body = json.dumps({"success": False, "error": {"type": "validation", "message": "UCP-Agent header required"}})

    assert A.check("accepted", body, None) == (False, "invalid_arguments")


def test_a_signature_rejection_is_reported_as_an_environment_problem():
    """A misconfigured allowlist is not the model choosing badly."""
    body = json.dumps({"success": False, "error": {"type": "signature", "message": "agent domain is not allowed"}})

    assert A.check("accepted", body, None) == (False, "tool_error")


def test_an_in_band_not_found_still_satisfies_the_accepted_tier():
    body = json.dumps({"success": False, "error": {"type": "not_found", "message": "Cart not found"}})

    assert A.check("accepted", body, None) == (True, None)
    assert A.check("data", body, None) == (False, "not_found")


def test_a_successful_body_is_untouched():
    assert A.check("accepted", json.dumps({"success": True, "data": {"id": "x"}}), None) == (True, None)
    assert A.inband_error(json.dumps({"success": True})) is None
    assert A.inband_error(json.dumps({"data": []})) is None
    assert A.inband_error("not json") is None


def test_an_unexecutable_tool_is_still_exempt():
    """`none` means the call never happened, so there is no body to judge."""
    body = json.dumps({"success": False, "error": {"type": "validation", "message": "nope"}})
    assert A.check("none", body, None) == (True, None)


def test_a_success_false_body_with_no_error_detail_still_fails():
    assert A.check("accepted", json.dumps({"success": False}), None)[0] is False


def test_violations_survive_into_the_error_string():
    """The message alone is a dead end: UCP answers a bad payload with
    'Validation failed for schema "checkout.create.request"' and puts the actual
    requirement in `violations`. Dropping it cost several rounds of guessing
    payload shapes by hand against a live server."""
    body = json.dumps(
        {
            "success": False,
            "error": {
                "type": "validation",
                "message": 'Validation failed for schema "checkout.create.request".',
                "violations": ["$.line_items is required"],
            },
        }
    )

    assert "$.line_items is required" in A.inband_error(body)


def test_an_error_without_violations_is_unchanged():
    body = json.dumps({"success": False, "error": {"type": "not_found", "message": "Cart not found."}})

    assert A.inband_error(body) == "not_found: Cart not found."
