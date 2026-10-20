"""Does the result format follow the protocol revision the client negotiated?

shopware/shopware#20948 renders every tool result through one renderer, and what
it sends depends on the revision:

  every revision    `isError` on a failure, a stable error code, and the
                    `shopware/generatedAt` stamp in the result `_meta`
  2025-06-18 on     `structuredContent` too: the data (wrapped as
                    `{"result": …}` when it is not a JSON object), or
                    `{"error": {"code", "message", "details"}}` on a failure

The rest of the suite negotiates one revision. This section opens a session per
revision in `FORMAT_REVISIONS` and calls the same probes in each, so a renderer
that sends `structuredContent` to a client that cannot read it, or forgets it for
one that can, fails here rather than in a client nobody tests.

The checks are written against `mcp_result`, not against the legacy text, so they
hold under the `v6.8.0.0` format as well. Under that format a client below
2025-06-18 gets no error code at all — the spec text carries only the message —
and that is accepted, not failed.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from eval.result_schema import JsonObject, as_object
from functional.reporting import Reporter
from mcp_client import Endpoint, mcp_call, mcp_init
from mcp_result import ToolOutcome, read_result

# Oldest, boundary, newest. The renderer changes behaviour at one revision only,
# so the ones in between add calls without adding information.
FORMAT_REVISIONS = ("2024-11-05", "2025-06-18", "2025-11-25")

# `McpToolResultRenderer::structuredContent()`: `isAtLeast(V2025_06_18)`.
STRUCTURED_SINCE = "2025-06-18"

PREDATES_RENDERER = (
    "the server predates shopware/shopware#20948: its results carry no shopware/generatedAt stamp, "
    "so there is no result format to check"
)

# A UUID no row has. Hex, 32 characters, so it passes validation and reaches the
# lookup: the point is the not-found code, not a rejected argument.
PHANTOM_ID = "0" * 32


@dataclass(frozen=True)
class Probe:
    """One call, and the outcome it must have: success, or a failure with `code`."""

    tool: str
    args: JsonObject
    label: str
    code: str | None = None


ADMIN_PROBES: tuple[Probe, ...] = (
    Probe("shopware-toolsets-list", {}, "toolsets-list"),
    # A list of rows, so the wrapping rule is exercised and not only the object case.
    Probe("shopware-entity-search", {"entity": "currency", "limit": 2}, "entity-search"),
    Probe("shopware-entity-schema", {"entity": "no_such_entity"}, "unknown entity", "invalid_arguments"),
    Probe("shopware-entity-read", {"entity": "product", "id": PHANTOM_ID}, "missing record", "not_found"),
)

# The Store endpoint has no entity tools; the meta-tools go through the same
# renderer, which is what is being checked.
STORE_PROBES: tuple[Probe, ...] = (Probe("shopware-toolsets-list", {}, "toolsets-list"),)


def expected_structured(outcome: ToolOutcome) -> object:
    """What `structuredContent` must be on the handshake era, given the result.

    Mirrors the renderer: a failure becomes the error object, an object is sent
    as is, anything else is wrapped. A decoded `{}` was a PHP object, which the
    renderer sends unwrapped.
    """
    if outcome.error is not None:
        error: JsonObject = {"message": outcome.error.message}
        if outcome.error.code is not None:
            error["code"] = outcome.error.code
        if outcome.error.details:
            error["details"] = outcome.error.details
        return {"error": error}
    data = as_object(outcome.data)
    if data or outcome.data == {}:
        return data
    return {"result": outcome.data}


def format_problems(outcome: ToolOutcome, probe: Probe, revision: str) -> list[str]:
    """Everything wrong with one probe's result on one revision; [] for nothing.

    Pure, so every rule is unit-tested without a server.
    """
    if outcome.shape == "protocol_error":
        return [f"the call was refused before the tool ran: {outcome.failure}"]

    problems: list[str] = []
    if outcome.generated_at is None:
        problems.append("no shopware/generatedAt in the result _meta")

    if probe.code is None:
        if not outcome.ok:
            problems.append(f"expected a result, the tool failed: {outcome.failure}")
        if outcome.is_error_flag:
            problems.append("isError is set on a result that succeeded")
    elif outcome.ok:
        problems.append(f"expected a failure with code {probe.code!r}, the call succeeded")
    else:
        if not outcome.is_error_flag:
            problems.append("isError is not set on a failure")
        code = outcome.error.code if outcome.error else None
        # The 6.8 text below 2025-06-18 carries the message only; there is no
        # code to compare, and its absence is the format, not a bug.
        code_unavailable = outcome.shape == "spec" and not outcome.has_structured
        if code != probe.code and not (code is None and code_unavailable):
            problems.append(f"error code {code!r}, expected {probe.code!r}")

    if revision >= STRUCTURED_SINCE:
        if not outcome.has_structured:
            problems.append(f"no structuredContent on {revision}")
        # Only when the call went the way it should: the structured copy of an
        # unexpected outcome is noise on top of the failure already reported.
        elif outcome.ok == (probe.code is None):
            structured = as_object(outcome.structured)
            want = expected_structured(outcome)
            # On a failure, the structured error is where the code came from, so
            # comparing it with itself would prove nothing; compare the parts the
            # text also carries.
            if outcome.error is not None:
                got_error = as_object(structured.get("error"))
                if got_error.get("message") != outcome.error.message:
                    problems.append(
                        f"structuredContent.error.message {got_error.get('message')!r} differs from the text"
                    )
                if probe.code is not None and got_error.get("code") != probe.code:
                    problems.append(f"structuredContent.error.code {got_error.get('code')!r}, expected {probe.code!r}")
            elif outcome.structured != want:
                problems.append("structuredContent does not carry the same data as the text")
    else:
        if outcome.has_structured:
            problems.append(f"structuredContent sent to a {revision} client, which predates it")
        if any(link.source == "resource_link" for link in outcome.links):
            problems.append(f"resource_link block sent to a {revision} client, which predates it")
    return problems


def verify_result_format(
    rep: Reporter,
    endpoint: Endpoint,
    probes: Sequence[Probe],
    revisions: Sequence[str] = FORMAT_REVISIONS,
) -> None:
    """Call every probe on a fresh session per revision and check its format."""
    rep.section("Result format per protocol revision (shopware/shopware#20948)")
    for revision in revisions:
        pinned = endpoint.with_protocol(revision)
        try:
            session, _ = mcp_init(endpoint=pinned)
        except RuntimeError as exc:
            rep.check_fail(f"result format {revision}: opens a session", str(exc)[:200])
            continue
        outcomes = [
            (probe, read_result(mcp_call(session, probe.tool, probe.args, endpoint=pinned))) for probe in probes
        ]

        # One stamp anywhere means the renderer is there. None at all, on the
        # first revision tried, means the server is older than the feature —
        # recorded once, with the reason, rather than failed probe by probe.
        if revision == revisions[0] and not any(o.generated_at for _, o in outcomes if o.shape != "protocol_error"):
            rep.check_skip("result format", PREDATES_RENDERER)
            return

        for probe, outcome in outcomes:
            label = f"result format {revision}: {probe.label} ({probe.tool})"
            problems = format_problems(outcome, probe, revision)
            if problems:
                rep.check_fail(label, "; ".join(problems))
            else:
                rep.check_pass(label)
