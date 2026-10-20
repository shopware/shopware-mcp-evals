#!/usr/bin/env python3
"""Did the call the model made actually work?

Grading a selection proves the model knew which tool to name. It does not prove
the call would have run: `shopware-entity-search` with a nonsense entity and a
malformed filter scores identically to one that returns products.

The demo data these run against is generated (`framework:demodata`), so every
assertion is structural. There is no fixed product to look for, and a fixture
asserting on one would fail the moment the seed changed.

Three tiers, because fixtures differ in how much can honestly be checked:

  data      the call should return something. Predicates on the payload.
  accepted  the arguments passed validation and the server took the call;
            empty or not-found is a legitimate outcome. This is the honest tier
            for a fixture that has to invent an id — the model cannot know a
            real UUID, so "not found" says the call was well-formed, which is
            the thing being tested.
  none      selection only. The escape hatch, for tools that cannot be executed
            at all (see toolclass.UNSAFE). Must be justified in the fixture's
            notes.

The distinction that carries the weight is between a call the server *rejected*
and one it *ran and returned nothing for*. The first is the model's fault and a
real failure; the second is the data's, and failing it would turn this into a
test of the seed.
"""

import re
from typing import cast

from eval.result_schema import ExpectSpec, JsonObject, as_object
from mcp_result import ToolOutcome

# Substrings that mark a server response as a rejection of the request rather
# than an answer to it. Matched case-insensitively against the error text.
# Deliberately narrow: anything not on this list is treated as the tool running
# and failing on its own terms, which is not the model's fault.
VALIDATION_MARKERS = (
    "validation",
    "invalid",
    "missing required",
    "required parameter",
    "unknown parameter",
    "unexpected parameter",
    "must be",
    "expected type",
    "malformed",
    "could not be decoded",
    "constraint",
)

# The addressed thing does not exist. Distinct from a malformed request: the
# call was shaped correctly, the id in it just does not resolve.
NOT_FOUND_MARKERS = (
    "not found",
    "no such",
    "does not exist",
    "could not be found",
    "unknown id",
    "no entity",
    "404",
    # shopware-entity-delete since shopware/shopware#20869: a delete whose ids
    # all miss is an error now, not a success with a notFound list. Phrased as
    # rows that do not "exist", so "does not exist" above never matched it.
    "none of these rows exist",
)

# UCP's error CODE, which prefixes every UCP error as `code: message`. The prose
# markers above matched every UCP not-found except one: get_order answers a
# guest with a deliberately non-leaking refusal — `not_found: Order "…" is not
# available to this request. A guest order can only be read back by the
# checkout session that placed it…` — whose prose names no absence at all. The
# code is the contract; the message is not. Missing it graded three correct
# get_order picks per nightly as tool_error, a third of every Store failure, on
# a refusal the UCP spec requires of a guest.
#
# Anchored to the start, not matched as a substring. `check()` tests not-found
# BEFORE validation — deliberately, see there — so an unanchored `not_found`
# would let `validation: status must not be not_found` pass a malformed call at
# the accepted tier. Only the code in the code position means the code.
UCP_NOT_FOUND = re.compile(r"^\s*not_found\s*:")

TIERS = ("data", "accepted", "none")


def is_validation_error(error: str | None) -> bool:
    """Whether an error means "your arguments were wrong".

    This is the line between a failure that belongs to the model and one that
    belongs to the environment. A 500, a timeout or a missing plugin is not
    evidence about tool descriptions.
    """
    if not error:
        return False
    lowered = str(error).lower()
    return any(marker in lowered for marker in VALIDATION_MARKERS)


def is_not_found(error: str | None) -> bool:
    """Whether an error means "that id does not resolve"."""
    if not error:
        return False
    lowered = str(error).lower()
    return bool(UCP_NOT_FOUND.match(lowered)) or any(marker in lowered for marker in NOT_FOUND_MARKERS)


def _resolve(payload: object, path: str) -> tuple[bool, object]:
    """Walk a dotted path. Returns (found, value) so a real null is not confused
    with an absent key — a tool returning `{"data": null}` has answered, and it
    has answered that there is nothing there."""
    current: object = payload
    for part in path.split("."):
        step = as_object(current)
        if part not in step:
            return False, None
        current = step[part]
    return True, current


def check(expect: str | ExpectSpec | None, outcome: ToolOutcome) -> tuple[bool, str | None]:
    """Evaluate one fixture's expectation. Returns (passed, failure_reason).

    `expect` is the fixture's `expect_result`: a tier name, or a mapping with a
    `tier` key plus predicates. `outcome` is the reply as mcp_result read it, so
    the predicates mean the same thing on every protocol revision and format.
    """
    # The mapping branch aliases rather than copies the caller's spec — nothing
    # here mutates it, so a defensive copy would only cost an allocation.
    spec: ExpectSpec = {"tier": expect} if isinstance(expect, str) else (expect or {})
    tier = spec.get("tier", "accepted")

    if tier == "none":
        return True, None

    # Every way to fail arrives as one message: a JSON-RPC error, `isError`, or
    # an in-band `{"success": false}` with HTTP 200 and no flag at all — how the
    # UCP and merchant tools answer. Checked after `none` so a tool that cannot
    # be executed is still exempt.
    #
    # The message is the one mcp_result DESCRIBED, never the raw body. With
    # `isError: true` the raw body is JSON starting with `{`, so the anchored
    # `not_found:` never matched it, and every correct get_order pick graded as
    # tool_error.
    error = outcome.failure

    if error:
        # Not-found is checked FIRST, before the validation markers, and that
        # ordering is deliberate.
        #
        # `accepted` exists precisely so a fixture that has to invent an id
        # ("checkout session co_xyz789") can still prove the call was
        # well-formed. The original code failed on *any* error, which made the
        # tier do the opposite of its stated purpose and failed every executed
        # Store fixture — 15 of 15 — while the ones never executed all passed.
        # Three runs were spent looking for a description problem that was
        # this.
        #
        # Order matters because "invalid" is a broad word that turns up inside
        # plenty of not-found messages ("Invalid or unknown checkout id"), and
        # of the two ways to be wrong, wrongly failing a fixture is worse: it
        # sends someone off to rewrite a description that was fine.
        if is_not_found(error):
            return (True, None) if tier == "accepted" else (False, "not_found")
        if is_validation_error(error):
            return False, "invalid_arguments"
        return False, "tool_error"

    if tier == "accepted":
        return True, None

    # Stored behind a pointer (shopware/shopware#20951): the reply carries a
    # preview and a URI, and the model is offered no resources/read to expand it.
    # Grading the preview would report `missing:data.…`, which reads as a tool
    # returning the wrong thing rather than as a result too large to deliver.
    if outcome.links:
        return False, "offloaded"

    payload = outcome.payload
    if payload is None:
        return False, "unreadable_result"

    for path in spec.get("has_keys", []):
        found, value = _resolve(payload, path)
        if not found or value is None:
            return False, f"missing:{path}"

    min_items = spec.get("min_items")
    if min_items:
        path, wanted = min_items.get("path", "data"), int(min_items.get("n", 1))
        found, value = _resolve(payload, path)
        if not found or not isinstance(value, list | dict | str):
            return False, f"not_a_collection:{path}"
        # Narrowed to list | dict | str above, but the element types stay unknown
        # for a decoded JSON value; only the length is being read.
        if len(cast(list[object] | dict[str, object] | str, value)) < wanted:
            return False, f"too_few:{path}<{wanted}"

    for needle in spec.get("contains", []):
        if needle not in outcome.text:
            return False, f"missing_text:{needle}"

    return True, None


def args_mismatch(expected: JsonObject | None, given: JsonObject) -> str | None:
    """How `given` misses a fixture's `expected_args`, or None if it matches.

    A subset match on top-level arguments, compared exactly. It exists for the
    fixtures where the tool alone proves nothing: "take this product out of that
    category" answered with `entity-delete` on `product` picks the right tool and
    deletes the product. Only the entity says which, and a dry-run executes
    either one cleanly, so no result tier can tell them apart.
    """
    for key, want in (expected or {}).items():
        if key not in given:
            return f"{key} missing, expected {want!r}"
        if given[key] != want:
            return f"{key} was {given[key]!r}, expected {want!r}"
    return None


def normalise(expect: str | ExpectSpec | None) -> ExpectSpec:
    """A fixture's `expect_result` as a mapping, defaulting to the `accepted`
    tier — the level a fixture with no declared expectation honestly supports."""
    if expect is None:
        return {"tier": "accepted"}
    return {"tier": expect} if isinstance(expect, str) else expect
