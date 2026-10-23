"""The result-format checks: one rule at a time, then the section end to end.

Replies are built in the shapes `McpToolResultRenderer` sends since
shopware/shopware#21844: the legacy envelope before 2025-06-18, the MCP format
with `structuredContent` from it on. The expected `structuredContent` is
written out by hand in every case rather than computed, so a test cannot agree
with the code because it shares the code's mistake.
"""

import json
from typing import cast, override

import pytest

from eval.result_schema import JsonObject, McpResponse, ToolDef, as_object
from functional import result_format as R
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint
from mcp_result import ToolOutcome, read_result

ADMIN: Endpoint = admin_endpoint()
STAMP: JsonObject = {"shopware/generatedAt": "2026-10-20T10:00:00+00:00"}

OK = R.Probe("shopware-toolsets-list", {}, "toolsets-list")
FAILS = R.Probe("shopware-entity-schema", {"entity": "x"}, "unknown entity", "invalid_arguments")

MESSAGE = "Entity x does not exist"
UNSET = object()


def response(result: JsonObject) -> McpResponse:
    return cast(McpResponse, cast(object, {"result": result}))


def legacy(
    envelope: JsonObject,
    *,
    structured: object = None,
    is_error: bool = False,
    meta: JsonObject | None = None,
    extra: list[object] | None = None,
) -> ToolOutcome:
    """A reply with the legacy envelope as its text, as a 2024-11-05 client gets it."""
    content: list[object] = [{"type": "text", "text": json.dumps(envelope)}, *(extra or [])]
    result: JsonObject = {"content": content, "isError": is_error, "_meta": STAMP if meta is None else meta}
    if structured is not None:
        result["structuredContent"] = structured
    return read_result(response(result))


def spec(
    data: object,
    structured: object = UNSET,
    *,
    meta: JsonObject | None = None,
    summary: str | None = None,
) -> ToolOutcome:
    """A success in the MCP format (`specTexts`): the data, the summary, the
    metadata block, and the metadata again under the prefix in the result `_meta`.
    `structured` defaults to the data as it is."""
    texts: list[str] = []
    if data is not None:
        texts.append(json.dumps(data))
    if summary is not None:
        texts.append(summary)
    if meta:
        texts.append(json.dumps({"_meta": meta}))
    result: JsonObject = {
        "content": [{"type": "text", "text": t} for t in texts or ["null"]],
        "isError": False,
        "_meta": {**{f"shopware/{k}": v for k, v in (meta or {}).items()}, **STAMP},
    }
    chosen = data if structured is UNSET else structured
    if chosen is not None:
        result["structuredContent"] = chosen
    return read_result(response(result))


def spec_failure(
    code: str | None, *, message: str = MESSAGE, structured_error: JsonObject | None = None
) -> ToolOutcome:
    """A failure in the MCP format: the message as text, the error as `structuredContent`."""
    error: JsonObject = {"message": message} if code is None else {"code": code, "message": message}
    result: JsonObject = {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "_meta": STAMP,
        "structuredContent": {"error": structured_error or error},
    }
    return read_result(response(result))


def legacy_failure(code: str | None, *, flag: bool = True) -> ToolOutcome:
    envelope: JsonObject = {"success": False, "error": MESSAGE}
    if code is not None:
        envelope["code"] = code
    return legacy(envelope, is_error=flag)


# --- expected_structured: success, per rule and revision -----------------------------


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25", "2026-07-28"])
def test_an_object_without_meta_is_sent_as_is(revision: str) -> None:
    assert R.expected_structured(spec({"a": 1}), revision) == {"a": 1}


def test_an_empty_object_is_an_object_and_not_wrapped() -> None:
    assert R.expected_structured(spec({}), "2025-06-18") == {}


@pytest.mark.parametrize(
    ("data", "revision"), [([1, 2], "2025-06-18"), ([1, 2], "2025-11-25"), (5, "2025-11-25"), ([], "2025-06-18")]
)
def test_a_non_object_without_meta_is_wrapped_while_structured_content_must_be_an_object(
    data: object, revision: str
) -> None:
    """`[]` is a PHP list, not an object, so it is wrapped too."""
    assert R.expected_structured(spec(data), revision) == {"result": data}


def test_a_non_object_without_meta_is_sent_as_is_from_2026_07_28() -> None:
    assert R.expected_structured(spec([1, 2]), "2026-07-28") == [1, 2]


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_an_object_with_meta_gains_the_model_meta_without_the_bookkeeping(revision: str) -> None:
    outcome = spec({"a": 1}, meta={"total": 3, "dryRun": True, "responseSize": 120, "resourceUri": "x"})

    assert R.expected_structured(outcome, revision) == {"a": 1, "_meta": {"total": 3, "dryRun": True}}


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25", "2026-07-28"])
def test_a_non_object_with_meta_is_wrapped_with_it_on_every_revision(revision: str) -> None:
    outcome = spec([1, 2], meta={"total": 2})

    assert R.expected_structured(outcome, revision) == {"result": [1, 2], "_meta": {"total": 2}}


def test_a_scalar_with_meta_is_wrapped_with_it() -> None:
    assert R.expected_structured(spec(7, meta={"dryRun": True}), "2025-11-25") == {
        "result": 7,
        "_meta": {"dryRun": True},
    }


def test_the_summary_joins_the_model_meta() -> None:
    outcome = spec({"a": 1}, meta={"total": 1}, summary="One row.")

    assert R.expected_structured(outcome, "2025-06-18") == {"a": 1, "_meta": {"total": 1, "summary": "One row."}}


def test_a_summary_alone_is_model_meta() -> None:
    assert R.expected_structured(spec([1], summary="One."), "2025-06-18") == {
        "result": [1],
        "_meta": {"summary": "One."},
    }


def test_a_summary_key_in_the_meta_wins_over_the_summary() -> None:
    outcome = spec({"a": 1}, meta={"summary": "from the tool"}, summary="the summary")

    assert R.expected_structured(outcome, "2025-06-18") == {"a": 1, "_meta": {"summary": "from the tool"}}


def test_meta_of_bookkeeping_only_adds_nothing() -> None:
    outcome = spec([1], meta={"responseSize": 12})

    assert R.expected_structured(outcome, "2025-06-18") == {"result": [1]}


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_an_object_with_its_own_meta_key_is_sent_as_is(revision: str) -> None:
    data: JsonObject = {"a": 1, "_meta": {"own": True}}

    assert R.expected_structured(spec(data, meta={"total": 1}), revision) == data


@pytest.mark.parametrize(("data", "want"), [({"a": 1}, {"a": 1}), ([1], {"result": [1]})])
def test_a_tool_with_an_output_schema_gets_no_model_meta(data: object, want: object) -> None:
    outcome = spec(data, meta={"total": 1}, summary="s")

    assert R.expected_structured(outcome, "2025-11-25", has_output_schema=True) == want


def test_a_result_without_data_has_no_structured_content() -> None:
    assert R.expected_structured(spec(None, summary="Stored behind a link.", meta={"total": 1}), "2025-06-18") is None


# --- expected_structured: failures ---------------------------------------------------


def test_a_failure_becomes_the_error_object_with_its_details() -> None:
    outcome = legacy({"success": False, "error": {"message": "bad", "field": "id"}, "code": "invalid_arguments"})

    assert R.expected_structured(outcome) == {
        "error": {"message": "bad", "code": "invalid_arguments", "details": {"field": "id"}}
    }


def test_a_failure_without_a_code_omits_it() -> None:
    assert R.expected_structured(legacy_failure(None)) == {"error": {"message": MESSAGE}}


# --- format_problems: the clean cases -----------------------------------------------


@pytest.mark.parametrize("data", [{"toolsets": []}, [1, 2], {}])
def test_a_success_without_meta_is_clean_on_every_revision(data: object) -> None:
    wrapped: object = data if data == {} or as_object(data) else {"result": data}

    assert R.format_problems(legacy({"success": True, "data": data}), OK, "2024-11-05") == []
    assert R.format_problems(spec(data, wrapped), OK, "2025-06-18") == []
    assert R.format_problems(spec(data, wrapped), OK, "2025-11-25") == []


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_a_success_with_meta_and_summary_is_clean(revision: str) -> None:
    outcome = spec(
        [{"id": "c1"}],
        {"result": [{"id": "c1"}], "_meta": {"total": 1, "summary": "One currency."}},
        meta={"total": 1, "responseSize": 40},
        summary="One currency.",
    )

    assert R.format_problems(outcome, OK, revision) == []


def test_a_legacy_success_with_meta_is_clean_below_the_boundary() -> None:
    outcome = legacy({"success": True, "data": [1], "_meta": {"total": 1}})

    assert R.format_problems(outcome, OK, "2024-11-05") == []


def test_an_object_with_its_own_meta_key_is_clean() -> None:
    data: JsonObject = {"a": 1, "_meta": {"own": True}}

    assert R.format_problems(spec(data, data, meta={"total": 1}), OK, "2025-06-18") == []


def test_a_tool_with_an_output_schema_is_clean_without_model_meta() -> None:
    outcome = spec({"a": 1}, {"a": 1}, meta={"total": 1})

    assert R.format_problems(outcome, OK, "2025-11-25", has_output_schema=True) == []


def test_a_result_without_data_is_clean_without_structured_content() -> None:
    assert R.format_problems(spec(None, summary="Nothing.", meta={"total": 0}), OK, "2025-06-18") == []


def test_a_coded_failure_is_clean_on_every_revision() -> None:
    assert R.format_problems(legacy_failure("invalid_arguments"), FAILS, "2024-11-05") == []
    assert R.format_problems(spec_failure("invalid_arguments"), FAILS, "2025-06-18") == []
    assert R.format_problems(spec_failure("invalid_arguments"), FAILS, "2025-11-25") == []


# --- format_problems: the format per revision ----------------------------------------


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_the_legacy_envelope_from_the_boundary_is_a_problem(revision: str) -> None:
    """What #20948 sent and #21844 replaced: the envelope, with structuredContent next to it."""
    outcome = legacy({"success": True, "data": {"a": 1}}, structured={"a": 1})

    assert R.format_problems(outcome, OK, revision) == [
        f"the legacy envelope sent to a {revision} client, which gets the MCP format"
    ]


def test_the_mcp_format_below_the_boundary_is_a_problem() -> None:
    """What the `v6.8.0.0` flag sent before #21844. The revision alone picks the
    format now, so the missing code is no longer excused either."""
    outcome = read_result(response({"content": [{"type": "text", "text": MESSAGE}], "isError": True, "_meta": STAMP}))

    assert outcome.shape == "spec"
    assert R.format_problems(outcome, FAILS, "2024-11-05") == [
        "error code None, expected 'invalid_arguments'",
        "the MCP format sent to a 2024-11-05 client, which gets the legacy envelope",
    ]


def test_structured_content_below_the_boundary_is_a_problem() -> None:
    outcome = legacy({"success": True, "data": {}}, structured={})

    assert R.format_problems(outcome, OK, "2025-03-26") == [
        "structuredContent sent to a 2025-03-26 client, which predates it"
    ]


def test_a_resource_link_below_the_boundary_is_a_problem() -> None:
    link: JsonObject = {"type": "resource_link", "uri": "shopware://tool-result/abc", "name": "tool-result"}
    outcome = legacy({"success": True, "data": {}}, extra=[link])

    assert R.format_problems(outcome, OK, "2024-11-05") == [
        "resource_link block sent to a 2024-11-05 client, which predates it"
    ]


# --- format_problems: structuredContent of a success ---------------------------------


def test_missing_structured_content_from_the_boundary_is_a_problem() -> None:
    assert R.format_problems(spec({}, None), OK, "2025-06-18") == ["no structuredContent on 2025-06-18"]


def test_structured_content_for_a_result_without_data_is_a_problem() -> None:
    outcome = spec(None, {"result": None}, summary="Nothing.")

    assert R.format_problems(outcome, OK, "2025-06-18") == ["structuredContent sent for a result without data"]


def test_structured_content_that_disagrees_with_the_text_is_a_problem() -> None:
    assert R.format_problems(spec([1, 2], {"result": [1]}), OK, "2025-11-25") == [R.DIFFERS]


def test_an_unwrapped_list_is_a_problem() -> None:
    assert R.format_problems(spec([1, 2], [1, 2]), OK, "2025-06-18") == [R.DIFFERS]


def test_missing_model_meta_is_named_on_its_own() -> None:
    """The #20948 shape: the data right, the metadata missing."""
    outcome = spec([1], {"result": [1]}, meta={"total": 1})

    assert R.format_problems(outcome, OK, "2025-06-18") == [
        "structuredContent._meta None is not the text's _meta block without resourceUri/responseSize, "
        "plus its summary: {'total': 1}"
    ]


def test_model_meta_that_disagrees_with_the_text_block_is_named_on_its_own() -> None:
    outcome = spec({"a": 1}, {"a": 1, "_meta": {"total": 2, "responseSize": 9}}, meta={"total": 1, "responseSize": 9})

    assert R.format_problems(outcome, OK, "2025-11-25") == [
        "structuredContent._meta {'total': 2, 'responseSize': 9} is not the text's _meta block without "
        "resourceUri/responseSize, plus its summary: {'total': 1}"
    ]


def test_data_and_meta_both_wrong_are_two_problems() -> None:
    outcome = spec([1, 2], {"result": [1], "_meta": {}}, meta={"total": 2})

    problems = R.format_problems(outcome, OK, "2025-06-18")

    assert problems[0] == R.DIFFERS
    assert len(problems) == 2


def test_model_meta_on_a_tool_with_an_output_schema_is_a_problem() -> None:
    outcome = spec({"a": 1}, {"a": 1, "_meta": {"total": 1}}, meta={"total": 1})

    assert R.format_problems(outcome, OK, "2025-11-25", has_output_schema=True) == [
        f"{R.DIFFERS}: it carries _meta, and the tool declares an outputSchema"
    ]


def test_model_meta_without_metadata_in_the_text_is_a_problem() -> None:
    outcome = spec({"a": 1}, {"a": 1, "_meta": {"total": 1}})

    assert R.format_problems(outcome, OK, "2025-06-18") == [
        f"{R.DIFFERS}: it carries _meta, and the text has no metadata for it"
    ]


def test_model_meta_folded_into_an_object_with_its_own_meta_key_is_a_problem() -> None:
    data: JsonObject = {"a": 1, "_meta": {"own": True}}
    outcome = spec(data, {"a": 1, "_meta": {"total": 1}}, meta={"total": 1})

    assert R.format_problems(outcome, OK, "2025-06-18") == [
        f"{R.DIFFERS}: it carries _meta, and the text has no metadata for it"
    ]


def test_a_tool_missing_from_tools_list_cannot_be_predicted() -> None:
    problems = R.format_problems(spec({"a": 1}), OK, "2025-06-18", has_output_schema=None)

    assert problems == [
        "shopware-toolsets-list is not in the session's tools/list, so whether it declares an outputSchema, "
        "and with it the structuredContent, is unknown"
    ]


def test_a_failure_needs_no_tools_list_entry() -> None:
    assert R.format_problems(spec_failure("invalid_arguments"), FAILS, "2025-06-18", has_output_schema=None) == []


# --- format_problems: everything else -------------------------------------------------


def test_a_protocol_error_is_reported_alone() -> None:
    outcome = read_result({"error": {"code": -32602, "message": "Unknown tool"}})

    assert R.format_problems(outcome, OK, "2025-06-18") == ["the call was refused before the tool ran: Unknown tool"]


def test_a_missing_stamp_is_a_problem() -> None:
    outcome = legacy({"success": True, "data": {}}, meta={})

    assert R.format_problems(outcome, OK, "2024-11-05") == ["no shopware/generatedAt in the result _meta"]


def test_an_unexpected_failure_names_the_error() -> None:
    assert R.format_problems(legacy_failure("tool_error"), OK, "2024-11-05") == [
        f"expected a result, the tool failed: {MESSAGE}",
        "isError is set on a result that succeeded",
    ]


def test_an_unexpected_success_is_a_problem() -> None:
    assert R.format_problems(legacy({"success": True, "data": {}}), FAILS, "2024-11-05") == [
        "expected a failure with code 'invalid_arguments', the call succeeded"
    ]


def test_a_failure_without_the_flag_is_a_problem() -> None:
    assert R.format_problems(legacy_failure("invalid_arguments", flag=False), FAILS, "2024-11-05") == [
        "isError is not set on a failure"
    ]


def test_the_wrong_code_is_a_problem() -> None:
    assert R.format_problems(legacy_failure("tool_error"), FAILS, "2024-11-05") == [
        "error code 'tool_error', expected 'invalid_arguments'"
    ]


def test_a_legacy_failure_without_a_code_is_a_problem() -> None:
    assert R.format_problems(legacy_failure(None), FAILS, "2024-11-05") == [
        "error code None, expected 'invalid_arguments'"
    ]


def test_a_failure_without_structured_content_from_the_boundary_is_a_problem() -> None:
    outcome = read_result(response({"content": [{"type": "text", "text": MESSAGE}], "isError": True, "_meta": STAMP}))

    assert "no structuredContent on 2025-06-18" in R.format_problems(outcome, FAILS, "2025-06-18")


def test_a_structured_error_that_disagrees_with_the_text_is_a_problem() -> None:
    outcome = spec_failure(
        "invalid_arguments", structured_error={"message": "something else", "code": "invalid_arguments"}
    )

    assert R.format_problems(outcome, FAILS, "2025-06-18") == [
        "structuredContent.error.message 'something else' differs from the text",
    ]


def test_the_structured_copy_of_an_unexpected_outcome_is_not_compared() -> None:
    assert R.format_problems(spec([1], {"result": [2]}), FAILS, "2025-06-18") == [
        "expected a failure with code 'invalid_arguments', the call succeeded"
    ]


# --- verify_result_format ---------------------------------------------------------------


class FakeServer:
    """Renders each probe the way #21844 does on the revision of its session,
    and lists the tools that session sees."""

    def __init__(
        self,
        answer: "dict[str, McpResponse] | None" = None,
        *,
        refuse: str | None = None,
        listed: "list[ToolDef] | None" = None,
        list_fails: bool = False,
    ) -> None:
        self.answer: dict[str, McpResponse] = answer or {}
        self.refuse: str | None = refuse
        self.listed: list[ToolDef] = (
            listed if listed is not None else [{"name": "shopware-toolsets-list"}, {"name": "shopware-entity-schema"}]
        )
        self.list_fails: bool = list_fails
        self.opened: list[str] = []
        self.pinned: list[tuple[str, ...]] = []
        self.sessions: dict[str, str] = {}

    def init(self, endpoint: Endpoint = ADMIN) -> tuple[str, str]:
        if endpoint.protocol_version == self.refuse:
            raise RuntimeError(f"Asked for protocol {self.refuse}, it negotiated 2024-11-05.")
        self.opened.append(endpoint.protocol_version)
        self.pinned.append(endpoint.toolsets)
        session = f"s{len(self.opened)}"
        self.sessions[session] = endpoint.protocol_version
        return session, ""

    def tools(self, session_id: str, endpoint: Endpoint = ADMIN) -> list[ToolDef]:
        assert self.sessions[session_id] == endpoint.protocol_version, "tools/list ran on another revision's session"
        if self.list_fails:
            raise RuntimeError("tools/list failed: boom")
        return self.listed

    def call(self, session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        del arguments
        assert self.sessions[session_id] == endpoint.protocol_version, "a probe ran on another revision's session"
        if tool in self.answer:
            return self.answer[tool]
        revision = endpoint.protocol_version
        modern = revision >= R.STRUCTURED_SINCE
        if tool == "shopware-toolsets-list":
            data: JsonObject = {"toolsets": []}
            meta: JsonObject = {"usage": "enable one"}
            if not modern:
                text = json.dumps({"success": True, "data": data, "_meta": meta})
                return response({"content": [{"type": "text", "text": text}], "isError": False, "_meta": STAMP})
            return response(
                {
                    "content": [
                        {"type": "text", "text": json.dumps(data)},
                        {"type": "text", "text": json.dumps({"_meta": meta})},
                    ],
                    "isError": False,
                    "structuredContent": {**data, "_meta": meta},
                    "_meta": {"shopware/usage": "enable one", **STAMP},
                }
            )
        if not modern:
            envelope: JsonObject = {"success": False, "error": MESSAGE, "code": "invalid_arguments"}
            return response(
                {"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": True, "_meta": STAMP}
            )
        return response(
            {
                "content": [{"type": "text", "text": MESSAGE}],
                "isError": True,
                "structuredContent": {"error": {"code": "invalid_arguments", "message": MESSAGE}},
                "_meta": STAMP,
            }
        )


def run(monkeypatch: pytest.MonkeyPatch, server: FakeServer, probes: tuple[R.Probe, ...] = (OK, FAILS)) -> Reporter:
    monkeypatch.setattr(R, "mcp_init", server.init)
    monkeypatch.setattr(R, "mcp_call", server.call)
    monkeypatch.setattr(R, "mcp_tools_list_all", server.tools)
    rep = Reporter("test", color=False)
    R.verify_result_format(rep, ADMIN, probes)
    return rep


def test_every_probe_passes_on_every_revision_against_a_correct_server(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer()
    rep = run(monkeypatch, server)

    assert server.opened == list(R.FORMAT_REVISIONS)
    assert (rep.passed, rep.failed, rep.skipped) == (len(R.FORMAT_REVISIONS) * 2, 0, 0)
    assert [r.get("label") for r in rep.records][:2] == [
        "result format 2024-11-05: toolsets-list (shopware-toolsets-list)",
        "result format 2024-11-05: unknown entity (shopware-entity-schema)",
    ]


def test_every_session_pins_all_toolsets_so_tools_list_names_the_deferred_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer()
    _ = run(monkeypatch, server)

    assert server.pinned == [("all",)] * len(R.FORMAT_REVISIONS)


def test_an_output_schema_in_tools_list_means_no_model_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake still adds `_meta`, so declaring a schema must turn its probe red from the boundary on."""
    schema: JsonObject = {"type": "object"}
    server = FakeServer(
        listed=[{"name": "shopware-toolsets-list", "outputSchema": schema}, {"name": "shopware-entity-schema"}]
    )
    rep = run(monkeypatch, server, probes=(OK,))

    assert [r.get("status") for r in rep.records] == ["pass", "fail", "fail"]
    assert "the tool declares an outputSchema" in str(rep.records[1].get("error"))


def test_a_probe_the_session_does_not_list_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(listed=[]), probes=(OK,))

    assert [r.get("status") for r in rep.records] == ["pass", "fail", "fail"]
    assert "is not in the session's tools/list" in str(rep.records[1].get("error"))


def test_a_tools_list_that_fails_is_its_own_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(list_fails=True), probes=(OK,))

    labels = {str(r.get("label")): r.get("status") for r in rep.records}
    assert labels["result format 2025-06-18: lists its tools"] == "fail"
    assert labels["result format 2025-06-18: toolsets-list (shopware-toolsets-list)"] == "fail"


def test_a_server_older_than_the_renderer_is_one_skip_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    unstamped: McpResponse = {"result": {"content": [{"type": "text", "text": '{"success": true, "data": {}}'}]}}
    server = FakeServer({"shopware-toolsets-list": unstamped, "shopware-entity-schema": unstamped})
    rep = run(monkeypatch, server)

    assert server.opened == ["2024-11-05"]
    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)
    assert rep.records[0].get("reason") == R.PREDATES_RENDERER
    assert rep.records[0].get("tool") == "check"


@pytest.mark.parametrize(
    "replies",
    [
        pytest.param("all-refused", id="every-probe-refused"),
        pytest.param("mixed", id="unstamped-and-refused"),
    ],
)
def test_a_refused_call_is_no_evidence_of_an_old_server(monkeypatch: pytest.MonkeyPatch, replies: str) -> None:
    """A JSON-RPC refusal never reached the renderer, so it cannot show the
    server predates it. The probes fail instead of the section being skipped."""
    refused: McpResponse = {"error": {"code": -32602, "message": "Tool is not enabled in your MCP allowlist."}}
    unstamped: McpResponse = {"result": {"content": [{"type": "text", "text": '{"success": true, "data": {}}'}]}}
    first = refused if replies == "all-refused" else unstamped
    server = FakeServer({"shopware-toolsets-list": first, "shopware-entity-schema": refused})
    rep = run(monkeypatch, server)

    assert server.opened == list(R.FORMAT_REVISIONS)
    assert not [r for r in rep.records if r.get("reason") == R.PREDATES_RENDERER]
    assert rep.failed == len(R.FORMAT_REVISIONS) * 2


def test_a_session_that_will_not_open_fails_that_revision_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(refuse="2025-06-18"))

    labels = {str(r.get("label")): r.get("status") for r in rep.records}
    assert labels["result format 2025-06-18: opens a session"] == "fail"
    assert rep.failed == 1
    assert rep.passed == 4


class Pre21844Server(FakeServer):
    """What the server sent before #21844: the legacy text on every revision,
    with the bare data as structuredContent from the boundary on."""

    @override
    def call(self, session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        del session_id, tool, arguments
        text = '{"success": true, "data": {}, "_meta": {"usage": "u"}}'
        result: JsonObject = {"content": [{"type": "text", "text": text}], "isError": False, "_meta": STAMP}
        if endpoint.protocol_version >= R.STRUCTURED_SINCE:
            result["structuredContent"] = {}
        return response(result)


def test_the_20948_rendering_fails_from_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """It passes the oldest revision only, and says why on the others."""
    rep = run(monkeypatch, Pre21844Server(), probes=(OK,))

    assert [r.get("status") for r in rep.records] == ["pass", "fail", "fail"]
    error = str(rep.records[1].get("error"))
    assert "the legacy envelope sent to a 2025-06-18 client" in error
    assert "structuredContent._meta None" in error


def test_the_check_skip_counts_and_records_a_reason() -> None:
    rep = Reporter("test", color=False)
    rep.check_skip("thing", "waits for shopware/shopware#1")

    assert rep.skipped == 1
    assert rep.records[0].get("status") == "skipped"
    assert rep.records[0].get("reason") == "waits for shopware/shopware#1"
