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


def _payload(result_text: str | None):
    try:
        return json.loads(result_text) if result_text else None
    except json.JSONDecodeError, TypeError:
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


def check(expect, result_text: str | None, error: str | None) -> tuple[bool, str | None]:
    """Evaluate one fixture's expectation. Returns (passed, failure_reason).

    `expect` is the fixture's `expect_result`: a tier name, or a mapping with a
    `tier` key plus predicates.
    """
    spec = {"tier": expect} if isinstance(expect, str) else dict(expect or {})
    tier = spec.get("tier", "accepted")

    if tier == "none":
        return True, None

    if error:
        # A rejected call is the model's fault; anything else is the
        # environment's and is reported so it can be excluded upstream.
        return False, "invalid_arguments" if is_validation_error(error) else "tool_error"

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


def normalise(expect) -> dict:
    """A fixture's `expect_result` as a mapping, defaulting to the `accepted`
    tier — the level a fixture with no declared expectation honestly supports."""
    if expect is None:
        return {"tier": "accepted"}
    return {"tier": expect} if isinstance(expect, str) else dict(expect)
