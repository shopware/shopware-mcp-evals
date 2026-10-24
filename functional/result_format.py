"""Does the result format follow the protocol revision the client negotiated?

shopware/shopware#20948 renders every tool result through one renderer, and what
it sends depends on the revision:

  every revision    `isError` on a failure, a stable error code, and the
                    `shopware/generatedAt` stamp in the result `_meta`
  2025-06-18 on     `structuredContent` too: the data (wrapped as
                    `{"result": …}` when it is not a JSON object), or
                    `{"error": {"code", "message", "details"}}` on a failure

shopware/shopware#21844 then made the revision pick the text as well:

  before 2025-06-18 the deprecated legacy `{"success": …}` envelope, unchanged
  2025-06-18 on     the MCP format: the plain data as JSON, the summary and
                    `{"_meta": …}` as blocks of their own, and the model's
                    metadata (`dryRun`, `total`, the summary) also inside
                    `structuredContent._meta`, because Claude Code, Codex and
                    VS Code show the model `structuredContent` and drop the text

The first is checked per probe and revision; the second once per run
("the format follows the revision"), because only replies on both sides of
2025-06-18 tell a server since #21844 from an older one that sends one format
everywhere. An older server records that as one skip naming the PR, and the
metadata checks with it.

The rest of the suite negotiates one revision. This section opens a session per
revision in `FORMAT_REVISIONS` and calls the same probes in each, so a renderer
that sends `structuredContent` to a client that cannot read it, or forgets it for
one that can, fails here rather than in a client nobody tests.

The checks are written against `mcp_result`, not against the legacy text, so they
hold under the `v6.8.0.0` format as well. Under that format a client below
2025-06-18 gets no error code at all — the spec text carries only the message —
and that is accepted, not failed.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from eval.result_schema import JsonObject, as_object
from functional.reporting import Reporter
from mcp_client import Endpoint, mcp_call, mcp_init
from mcp_result import ToolOutcome, format_for, format_verdict, read_result

# Oldest, boundary, newest. The renderer changes behaviour at one revision only,
# so the ones in between add calls without adding information.
FORMAT_REVISIONS = ("2024-11-05", "2025-06-18", "2025-11-25")

# `McpToolResultRenderer::structuredContent()`: `isAtLeast(V2025_06_18)`.
STRUCTURED_SINCE = "2025-06-18"

PREDATES_RENDERER = (
    "the server predates shopware/shopware#20948: its results carry no shopware/generatedAt stamp, "
    "so there is no result format to check"
)

FORMAT_FIX = "shopware/shopware#21844"
FORMAT_FIX_URL = "https://github.com/shopware/shopware/pull/21844"
FOLLOWS_REVISION = f"result format follows the revision ({FORMAT_FIX})"
PREDATES_REVISION_FORMAT = (
    f"the server predates {FORMAT_FIX} ({FORMAT_FIX_URL}): it sends one result format on every revision, "
    "so neither the format per revision nor the model metadata in structuredContent can be checked"
)
MODEL_META_IN_STRUCTURED = "model metadata in structuredContent"
MODEL_META_IN_TEXT = "model metadata in the legacy text"
NO_STRUCTURED_META = "no _meta in structuredContent"
NO_MODEL_META_IN_STRUCTURED = "no model metadata, no _meta in structuredContent"

# A UUID no row has. Hex, 32 characters, so it passes validation and reaches the
# lookup: the point is the not-found code, not a rejected argument.
PHANTOM_ID = "0" * 32
# A tax rate that does not exist, upserted as a dry run: the write is rolled
# back, and the reply is the one dry-run result every lane can produce.
DRY_RUN_TAX: JsonObject = {"id": "0" * 31 + "1", "name": "mcp-evals dry run", "taxRate": 1}


@dataclass(frozen=True)
class Probe:
    """One call, and the outcome it must have: success, or a failure with `code`."""

    tool: str
    args: JsonObject
    label: str
    code: str | None = None
    # Metadata keys the model has to be shown, in the text's `_meta` on every
    # revision and in `structuredContent._meta` from 2025-06-18 on.
    meta: tuple[str, ...] = ()


ADMIN_PROBES: tuple[Probe, ...] = (
    Probe("shopware-toolsets-list", {}, "toolsets-list"),
    # A list of rows, so the wrapping rule is exercised and not only the object case.
    Probe("shopware-entity-search", {"entity": "currency", "limit": 2}, "entity-search", meta=("total",)),
    Probe(
        "shopware-entity-upsert",
        {"entity": "tax", "payload": json.dumps([DRY_RUN_TAX]), "dryRun": True},
        "dry-run upsert",
        meta=("dryRun",),
    ),
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
            # The model's `_meta` next to the data is checked by
            # `metadata_problems`; here only the data has to agree.
            elif outcome.structured_data != want:
                problems.append("structuredContent does not carry the same data as the text")
    else:
        if outcome.has_structured:
            problems.append(f"structuredContent sent to a {revision} client, which predates it")
        if any(link.source == "resource_link" for link in outcome.links):
            problems.append(f"resource_link block sent to a {revision} client, which predates it")
    return problems


def metadata_problems(outcome: ToolOutcome, probe: Probe, revision: str) -> list[str]:
    """What is wrong with the metadata a model is shown, on a server since
    shopware/shopware#21844; [] for nothing. Only for a probe that worked.

    Legacy era: the probe's keys in the envelope's `_meta`, the only place an
    old client has. MCP format: the same keys in the `{"_meta": …}` text block,
    and `structuredContent._meta` holding exactly the model's metadata (the
    result's metadata without `responseSize` and `resourceUri`, plus the
    summary), or no such key when there is none.
    """
    problems = [f"no {key} in the text's _meta" for key in probe.meta if key not in outcome.meta]
    if format_for(revision) == "legacy" or outcome.data_has_meta_key:
        return problems
    want = outcome.model_meta
    got = outcome.structured_meta
    if not want:
        if got is not None:
            problems.append(f"a _meta in structuredContent for a result without metadata: {got}")
    elif got is None:
        problems.append(f"{NO_STRUCTURED_META}, expected {json.dumps(want, sort_keys=True)[:200]}")
    elif got != want:
        problems.append(
            f"structuredContent._meta {json.dumps(got, sort_keys=True)[:200]} differs from the metadata "
            f"{json.dumps(want, sort_keys=True)[:200]}"
        )
    return problems


def _report(rep: Reporter, label: str, problems: list[str]) -> None:
    if problems:
        rep.check_fail(label, "; ".join(problems))
    else:
        rep.check_pass(label)


def verify_result_format(
    rep: Reporter,
    endpoint: Endpoint,
    probes: Sequence[Probe],
    revisions: Sequence[str] = FORMAT_REVISIONS,
) -> None:
    """Call every probe on a fresh session per revision and check its format."""
    rep.section("Result format per protocol revision (shopware/shopware#20948, #21844)")
    replies: list[tuple[str, Probe, ToolOutcome]] = []
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
            _report(
                rep,
                f"result format {revision}: {probe.label} ({probe.tool})",
                format_problems(outcome, probe, revision),
            )
            replies.append((revision, probe, outcome))

    verify_format_per_revision(rep, replies)


def verify_format_per_revision(rep: Reporter, replies: Sequence[tuple[str, Probe, ToolOutcome]]) -> None:
    """The text format follows the revision, and the model sees the metadata."""
    verdict = format_verdict((revision, outcome) for revision, _, outcome in replies)
    if verdict == "one_format":
        rep.check_skip(FOLLOWS_REVISION, PREDATES_REVISION_FORMAT)
        return
    if verdict == "no_format":
        rep.check_fail(FOLLOWS_REVISION, "no reply came back in the legacy or the spec format, so none shows it")
        return
    wrong = [
        f"{revision} {probe.label}: {outcome.shape}, expected {format_for(revision)}"
        for revision, probe, outcome in replies
        if outcome.shape in ("legacy", "spec") and outcome.shape != format_for(revision)
    ]
    _report(rep, FOLLOWS_REVISION, wrong)

    for revision, probe, outcome in replies:
        # A failure has no metadata to show, and a call that went wrong has
        # been reported above already.
        if not outcome.ok or probe.code is not None:
            continue
        spec = format_for(revision) == "spec"
        if not spec and not probe.meta:
            continue
        where = MODEL_META_IN_TEXT
        if spec:
            # A result without metadata gets a label of its own: passing it says
            # nothing about whether the metadata reaches structuredContent.
            where = MODEL_META_IN_STRUCTURED if outcome.model_meta else NO_MODEL_META_IN_STRUCTURED
        _report(rep, f"{where} {revision}: {probe.label} ({probe.tool})", metadata_problems(outcome, probe, revision))
