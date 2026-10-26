"""Does the result format follow the protocol revision the client negotiated?

shopware/shopware#20948 renders every tool result through one renderer, and
shopware/shopware#21844 made the negotiated revision the only thing that picks
the format:

  every revision    `isError` on a failure, a stable error code, and the
                    `shopware/generatedAt` stamp in the result `_meta`
  before 2025-06-18 the legacy `{"success": …}` envelope as the only text
                    block, and no `structuredContent`
  2025-06-18 on     the MCP format: the data as JSON, then the summary, then
                    `{"_meta": …}` as text blocks, and `structuredContent`:
                    the data, plus the metadata for the model under `_meta`
                    (`expected_structured`), or
                    `{"error": {"code", "message", "details"}}` on a failure

The rest of the suite negotiates one revision. This section opens a session per
revision in `FORMAT_REVISIONS` and calls the same probes in each, so a renderer
that sends `structuredContent` to a client that cannot read it, or forgets it for
one that can, fails here rather than in a client nobody tests.

Each session pins every toolset (`?toolsets=all`), so its own `tools/list` says
whether a probed tool declares an `outputSchema`: such a tool's
`structuredContent` is its data and nothing else.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from eval.result_schema import JsonObject, as_object
from functional.reporting import Reporter
from mcp_client import Endpoint, mcp_call, mcp_init, mcp_tools_list_all
from mcp_result import TOOL_RESULT_URI_PREFIX, ToolOutcome, read_result

# Oldest, boundary, newest. The renderer changes behaviour at one revision only,
# so the ones in between add calls without adding information.
FORMAT_REVISIONS = ("2024-11-05", "2025-06-18", "2025-11-25")

# `McpToolResultCapabilities`: `specFormat` from this revision on.
STRUCTURED_SINCE = "2025-06-18"

# `McpToolResultCapabilities`: `objectOnlyStructuredContent` until this one. A
# list or scalar without metadata is wrapped as `{"result": …}` before it and
# sent as it is from it on.
ANY_VALUE_SINCE = "2026-07-28"

# `McpToolResultRenderer::STRUCTURED_META_KEY` and `INTERNAL_META_KEYS`.
STRUCTURED_META_KEY = "_meta"
INTERNAL_META_KEYS = frozenset({"responseSize", "resourceUri"})

PREDATES_RENDERER = (
    "the server predates shopware/shopware#20948: its results carry no shopware/generatedAt stamp, "
    "so there is no result format to check"
)

DIFFERS = "structuredContent does not carry the same data as the text"

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


def _object(value: object) -> JsonObject | None:
    """`value` as a JSON object, or None for anything else: `{}` is one, `[]` is not."""
    return cast(JsonObject, value) if isinstance(value, dict) else None


def offloaded(outcome: ToolOutcome) -> bool:
    """Whether the result is stored behind a pointer. A link of the tool's own
    is not one: `McpToolResultRenderer::isLinkedResult()` asks for the stored
    result's link only."""
    return any(link.uri.startswith(TOOL_RESULT_URI_PREFIX) for link in outcome.links)


def model_meta(outcome: ToolOutcome) -> JsonObject:
    """`McpToolResultRenderer::modelMeta()`, from what the text carries: the
    `{"_meta": …}` block without the client's bookkeeping, and the summary
    unless the metadata already has a `summary` of its own."""
    meta: JsonObject = {key: value for key, value in outcome.meta.items() if key not in INTERNAL_META_KEYS}
    if outcome.summary and "summary" not in meta:
        meta["summary"] = outcome.summary
    return meta


def adds_model_meta(outcome: ToolOutcome, has_output_schema: bool) -> bool:
    """Whether `structuredContent` gains the reserved `_meta` key: there is
    metadata for the model, no `outputSchema` fixes the shape, and the data is
    not an object with a `_meta` key of its own."""
    if has_output_schema or not model_meta(outcome):
        return False
    data = _object(outcome.data)
    return data is None or STRUCTURED_META_KEY not in data


def expected_structured(
    outcome: ToolOutcome, revision: str = STRUCTURED_SINCE, has_output_schema: bool = False
) -> object:
    """What `structuredContent` must be on `revision`, given the result; None
    for a result that has none.

    Mirrors `McpToolResultRenderer::structuredContent()`. A failure becomes the
    error object. An offloaded success has none, and neither has one without
    data or model metadata. Otherwise the data, null included, carries
    the model metadata under `_meta` when `adds_model_meta` says so, an object
    next to its own keys and anything else as `{"result": …, "_meta": …}`.
    Without it, an object is sent as is, and anything else is wrapped as
    `{"result": …}` up to `ANY_VALUE_SINCE`. A decoded `{}` was a PHP object,
    so it counts as an object; `[]` is a list.
    """
    if outcome.error is not None:
        error: JsonObject = {"message": outcome.error.message}
        if outcome.error.code is not None:
            error["code"] = outcome.error.code
        if outcome.error.details:
            error["details"] = outcome.error.details
        return {"error": error}
    data = outcome.data
    # An offloaded result's data is the stored resource, so nothing is left to
    # structure. Any other result without data keeps its model metadata, as
    # `{"result": null, "_meta": …}` (shopware/shopware@7f64703f325).
    if data is None and (offloaded(outcome) or not adds_model_meta(outcome, has_output_schema)):
        return None
    obj = _object(data)
    if adds_model_meta(outcome, has_output_schema):
        meta = model_meta(outcome)
        return {**obj, STRUCTURED_META_KEY: meta} if obj is not None else {"result": data, STRUCTURED_META_KEY: meta}
    if obj is not None or revision >= ANY_VALUE_SINCE:
        return data
    return {"result": data}


def _without_meta(value: object) -> object:
    obj = _object(value)
    return value if obj is None else {key: item for key, item in obj.items() if key != STRUCTURED_META_KEY}


def structured_problems(outcome: ToolOutcome, revision: str, has_output_schema: bool) -> list[str]:
    """Everything wrong with the `structuredContent` of a success; [] for nothing.

    The model metadata is compared on its own, against the text's `_meta`
    block, so a reply whose data agrees and whose metadata does not says so
    rather than reading as different data.
    """
    want = expected_structured(outcome, revision, has_output_schema)
    if want is None:
        if not outcome.has_structured:
            return []
        reason = "an offloaded result" if offloaded(outcome) else "a result without data or metadata"
        return [f"structuredContent sent for {reason}"]
    if not outcome.has_structured:
        return [f"no structuredContent on {revision}"]
    got = _object(outcome.structured)
    if not adds_model_meta(outcome, has_output_schema):
        if outcome.structured == want:
            return []
        if got is not None and STRUCTURED_META_KEY in got:
            reason = "the tool declares an outputSchema" if has_output_schema else "the text has no metadata for it"
            return [f"{DIFFERS}: it carries _meta, and {reason}"]
        return [DIFFERS]
    problems: list[str] = []
    if got is None or _without_meta(got) != _without_meta(want):
        problems.append(DIFFERS)
    got_meta = got.get(STRUCTURED_META_KEY) if got is not None else None
    if got_meta != model_meta(outcome):
        problems.append(
            f"structuredContent._meta {got_meta!r} is not the text's _meta block without "
            f"{'/'.join(sorted(INTERNAL_META_KEYS))}, plus its summary: {model_meta(outcome)!r}"
        )
    return problems


def format_problems(
    outcome: ToolOutcome, probe: Probe, revision: str, has_output_schema: bool | None = False
) -> list[str]:
    """Everything wrong with one probe's result on one revision; [] for nothing.

    `has_output_schema` is None when the session's `tools/list` does not name
    the tool, so its `structuredContent` cannot be predicted. Pure, so every
    rule is unit-tested without a server.
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
        if code != probe.code:
            problems.append(f"error code {code!r}, expected {probe.code!r}")

    as_expected = outcome.ok == (probe.code is None)
    if revision >= STRUCTURED_SINCE:
        if outcome.shape == "legacy":
            problems.append(f"the legacy envelope sent to a {revision} client, which gets the MCP format")
        if outcome.error is not None:
            if not outcome.has_structured:
                problems.append(f"no structuredContent on {revision}")
            # On a failure, the structured error is where the parsed code and
            # message came from, so comparing it with them would prove nothing;
            # compare it with the text. The MCP format sends the message as the
            # first block, the legacy envelope under `error`.
            elif as_expected:
                got_error = as_object(as_object(outcome.structured).get("error"))
                text_message = outcome.texts[0] if outcome.shape == "spec" and outcome.texts else outcome.error.message
                if got_error.get("message") != text_message:
                    problems.append(
                        f"structuredContent.error.message {got_error.get('message')!r} differs from the text"
                    )
                if probe.code is not None and got_error.get("code") != probe.code:
                    problems.append(f"structuredContent.error.code {got_error.get('code')!r}, expected {probe.code!r}")
        # Only when the call went the way it should: the structured copy of an
        # unexpected outcome is noise on top of the failure already reported.
        elif as_expected:
            if has_output_schema is None:
                problems.append(
                    f"{probe.tool} is not in the session's tools/list, so whether it declares an outputSchema, "
                    "and with it the structuredContent, is unknown"
                )
            else:
                problems.extend(structured_problems(outcome, revision, has_output_schema))
    else:
        if outcome.shape == "spec":
            problems.append(f"the MCP format sent to a {revision} client, which gets the legacy envelope")
        if outcome.has_structured:
            problems.append(f"structuredContent sent to a {revision} client, which predates it")
        if any(link.source == "resource_link" for link in outcome.links):
            problems.append(f"resource_link block sent to a {revision} client, which predates it")
    return problems


def output_schemas(session: str, endpoint: Endpoint) -> dict[str, bool]:
    """Every tool the session lists, and whether it declares an `outputSchema`."""
    return {tool["name"]: "outputSchema" in tool for tool in mcp_tools_list_all(session, endpoint)}


def verify_result_format(
    rep: Reporter,
    endpoint: Endpoint,
    probes: Sequence[Probe],
    revisions: Sequence[str] = FORMAT_REVISIONS,
) -> None:
    """Call every probe on a fresh session per revision and check its format."""
    rep.section("Result format per protocol revision (shopware/shopware#20948, #21844)")
    for revision in revisions:
        # Every toolset pinned, so the deferred probes are in this session's
        # tools/list with their outputSchema. Visibility only: the calls are
        # the same either way.
        pinned = endpoint.with_protocol(revision).with_toolsets("all")
        try:
            session, _ = mcp_init(endpoint=pinned)
        except RuntimeError as exc:
            rep.check_fail(f"result format {revision}: opens a session", str(exc)[:200])
            continue
        try:
            schemas = output_schemas(session, pinned)
        except RuntimeError as exc:
            rep.check_fail(f"result format {revision}: lists its tools", str(exc)[:200])
            schemas = {}
        outcomes = [
            (probe, read_result(mcp_call(session, probe.tool, probe.args, endpoint=pinned))) for probe in probes
        ]

        # One stamp anywhere means the renderer is there. None at all, on the
        # first revision tried, means the server is older than the feature —
        # recorded once, with the reason, rather than failed probe by probe.
        # Only when every probe reached the renderer: a call refused before the
        # tool ran says nothing about the server's age, and fails below.
        rendered = [o for _, o in outcomes if o.shape != "protocol_error"]
        if (
            revision == revisions[0]
            and rendered
            and len(rendered) == len(outcomes)
            and not any(o.generated_at for o in rendered)
        ):
            rep.check_skip("result format", PREDATES_RENDERER)
            return

        for probe, outcome in outcomes:
            label = f"result format {revision}: {probe.label} ({probe.tool})"
            problems = format_problems(outcome, probe, revision, schemas.get(probe.tool))
            if problems:
                rep.check_fail(label, "; ".join(problems))
            else:
                rep.check_pass(label)
