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

import json
from typing import TypedDict

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
)

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
    return any(marker in lowered for marker in NOT_FOUND_MARKERS)


def _payload(result_text: str | None):
    try:
        return json.loads(result_text) if result_text else None
    except (json.JSONDecodeError, TypeError):
        return None


def _resolve(payload, path: str):
    """Walk a dotted path. Returns (found, value) so a real null is not confused
    with an absent key — a tool returning `{"data": null}` has answered, and it
    has answered that there is nothing there."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def inband_error(result_text: str | None) -> str | None:
    """The message from a `{"success": false}` body, or None if there is none.

    UCP tools answer a rejected request with HTTP 200 and an MCP result that
    carries `success: false` — there is no transport error at all, so
    `mcp_call_error` returns "" and the call looks clean. Without this the
    `accepted` tier passed every one of them: the entire Store suite would have
    gone green while not a single tool did anything, which is a worse outcome
    than the failure it replaced.
    """
    payload = _payload(result_text)
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        # `type` carries the classification the server already made — keeping it
        # in the string lets the markers below sort it without a second scheme.
        message = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ").strip()
        # `violations` is where the answer actually is. UCP's schema errors say
        # only 'Validation failed for schema "checkout.create.request"' in the
        # message, and carry `["$.line_items is required"]` alongside it — the
        # difference between a dead end and a fix. Dropping it cost several
        # rounds of guessing payload shapes by hand.
        violations = err.get("violations")
        if isinstance(violations, list) and violations:
            message += f" ({'; '.join(str(v) for v in violations)})"
        return message
    return str(err) if err else "the tool reported success: false"


class MinItems(TypedDict, total=False):
    """The `min_items` predicate: the collection at `path` must hold >= `n`."""

    path: str
    n: int


class ExpectSpec(TypedDict, total=False):
    """A fixture's `expect_result` in mapping form — a tier plus the predicates
    that tier turns on. `total=False`: a spec names only what it asserts, and the
    runner defaults the tier to `accepted`. Modelling it (rather than a bare dict)
    is what lets the type checker keep `min_items.get("n")` an int and reject a
    predicate key that no branch below reads."""

    tier: str
    has_keys: list[str]
    min_items: MinItems
    contains: list[str]


def check(expect: str | ExpectSpec | None, result_text: str | None, error: str | None) -> tuple[bool, str | None]:
    """Evaluate one fixture's expectation. Returns (passed, failure_reason).

    `expect` is the fixture's `expect_result`: a tier name, or a mapping with a
    `tier` key plus predicates.
    """
    # The mapping branch aliases rather than copies the caller's spec — nothing
    # here mutates it, so a defensive copy would only cost an allocation.
    spec: ExpectSpec = {"tier": expect} if isinstance(expect, str) else (expect or {})
    tier = spec.get("tier", "accepted")

    if tier == "none":
        return True, None

    # An in-band failure is a failure. Checked after `none` so a tool that
    # cannot be executed is still exempt, and folded into `error` so it sorts
    # through exactly the same not-found / malformed / environment logic rather
    # than growing a parallel one.
    error = error or inband_error(result_text)

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

    payload = _payload(result_text)
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
        if len(value) < wanted:
            return False, f"too_few:{path}<{wanted}"

    for needle in spec.get("contains", []):
        if needle not in (result_text or ""):
            return False, f"missing_text:{needle}"

    return True, None


def normalise(expect: str | ExpectSpec | None) -> ExpectSpec:
    """A fixture's `expect_result` as a mapping, defaulting to the `accepted`
    tier — the level a fixture with no declared expectation honestly supports."""
    if expect is None:
        return {"tier": "accepted"}
    return {"tier": expect} if isinstance(expect, str) else expect
