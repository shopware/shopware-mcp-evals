"""The result-format checks: one rule at a time, then the section end to end.

Replies are built in the shapes `McpToolResultRenderer` sends, so the rules are
tested against what a server on each revision really answers.
"""

import json
from typing import cast

import pytest

from eval.result_schema import JsonObject, McpResponse, as_object
from functional import result_format as R
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint
from mcp_result import ToolOutcome, read_result

ADMIN: Endpoint = admin_endpoint()
STAMP: JsonObject = {"shopware/generatedAt": "2026-10-20T10:00:00+00:00"}

OK = R.Probe("shopware-toolsets-list", {}, "toolsets-list")
FAILS = R.Probe("shopware-entity-schema", {"entity": "x"}, "unknown entity", "invalid_arguments")


def response(result: JsonObject) -> McpResponse:
    return cast(McpResponse, cast(object, {"result": result}))


def reply(
    envelope: JsonObject,
    *,
    structured: object = None,
    is_error: bool = False,
    meta: JsonObject | None = None,
    extra: list[object] | None = None,
) -> ToolOutcome:
    """A tools/call reply with the legacy envelope as text, as 6.7 renders it."""
    content: list[object] = [{"type": "text", "text": json.dumps(envelope)}, *(extra or [])]
    result: JsonObject = {"content": content, "isError": is_error, "_meta": STAMP if meta is None else meta}
    if structured is not None:
        result["structuredContent"] = structured
    return read_result(response(result))


def success(data: object, *, structured: bool) -> ToolOutcome:
    wrapped: JsonObject = as_object(data) if data == {} or as_object(data) else {"result": data}
    return reply({"success": True, "data": data}, structured=wrapped if structured else None)


def failure(code: str | None, *, structured: bool, flag: bool = True) -> ToolOutcome:
    envelope: JsonObject = {"success": False, "error": "Entity x does not exist"}
    error: JsonObject = {"message": "Entity x does not exist"}
    if code is not None:
        envelope["code"] = code
        error["code"] = code
    return reply(envelope, structured={"error": error} if structured else None, is_error=flag)


# --- expected_structured ------------------------------------------------------


def test_an_object_is_sent_as_is() -> None:
    assert R.expected_structured(success({"a": 1}, structured=False)) == {"a": 1}


def test_an_empty_object_is_not_wrapped() -> None:
    assert R.expected_structured(success({}, structured=False)) == {}


def test_a_list_is_wrapped() -> None:
    assert R.expected_structured(success([1, 2], structured=False)) == {"result": [1, 2]}


def test_a_failure_becomes_the_error_object_with_its_details() -> None:
    outcome = reply({"success": False, "error": {"message": "bad", "field": "id"}, "code": "invalid_arguments"})

    assert R.expected_structured(outcome) == {
        "error": {"message": "bad", "code": "invalid_arguments", "details": {"field": "id"}}
    }


def test_a_failure_without_a_code_omits_it() -> None:
    assert R.expected_structured(failure(None, structured=False)) == {"error": {"message": "Entity x does not exist"}}


# --- format_problems: the clean cases ----------------------------------------


@pytest.mark.parametrize("data", [{"toolsets": []}, [1, 2], {}])
def test_a_success_is_clean_on_every_revision(data: object) -> None:
    assert R.format_problems(success(data, structured=False), OK, "2024-11-05") == []
    assert R.format_problems(success(data, structured=True), OK, "2025-06-18") == []


def test_a_coded_failure_is_clean_on_every_revision() -> None:
    assert R.format_problems(failure("invalid_arguments", structured=False), FAILS, "2024-11-05") == []
    assert R.format_problems(failure("invalid_arguments", structured=True), FAILS, "2025-11-25") == []


def test_the_spec_format_below_the_boundary_may_carry_no_code() -> None:
    """The 6.8 text gives an old client the message only; that is the format."""
    outcome = read_result(
        {"result": {"content": [{"type": "text", "text": "Entity x does not exist"}], "isError": True, "_meta": STAMP}}
    )

    assert outcome.shape == "spec"
    assert R.format_problems(outcome, FAILS, "2024-11-05") == []


# --- format_problems: each rule ----------------------------------------------


def test_a_protocol_error_is_reported_alone() -> None:
    outcome = read_result({"error": {"code": -32602, "message": "Unknown tool"}})

    assert R.format_problems(outcome, OK, "2025-06-18") == ["the call was refused before the tool ran: Unknown tool"]


def test_a_missing_stamp_is_a_problem() -> None:
    outcome = reply({"success": True, "data": {}}, meta={})

    assert R.format_problems(outcome, OK, "2024-11-05") == ["no shopware/generatedAt in the result _meta"]


def test_an_unexpected_failure_names_the_error() -> None:
    problems = R.format_problems(failure("tool_error", structured=False), OK, "2024-11-05")

    assert problems == [
        "expected a result, the tool failed: Entity x does not exist",
        "isError is set on a result that succeeded",
    ]


def test_an_unexpected_success_is_a_problem() -> None:
    problems = R.format_problems(success({}, structured=False), FAILS, "2024-11-05")

    assert problems == ["expected a failure with code 'invalid_arguments', the call succeeded"]


def test_a_failure_without_the_flag_is_a_problem() -> None:
    problems = R.format_problems(failure("invalid_arguments", structured=False, flag=False), FAILS, "2024-11-05")

    assert problems == ["isError is not set on a failure"]


def test_the_wrong_code_is_a_problem() -> None:
    problems = R.format_problems(failure("tool_error", structured=False), FAILS, "2024-11-05")

    assert problems == ["error code 'tool_error', expected 'invalid_arguments'"]


def test_a_legacy_failure_without_a_code_is_a_problem() -> None:
    """Only the spec text is excused; the legacy envelope always has room for it."""
    problems = R.format_problems(failure(None, structured=False), FAILS, "2024-11-05")

    assert problems == ["error code None, expected 'invalid_arguments'"]


def test_missing_structured_content_from_the_boundary_is_a_problem() -> None:
    problems = R.format_problems(success({}, structured=False), OK, "2025-06-18")

    assert problems == ["no structuredContent on 2025-06-18"]


def test_structured_content_that_disagrees_with_the_text_is_a_problem() -> None:
    outcome = reply({"success": True, "data": [1, 2]}, structured={"result": [1]})

    assert R.format_problems(outcome, OK, "2025-11-25") == [
        "structuredContent does not carry the same data as the text"
    ]


def test_an_unwrapped_list_is_a_problem() -> None:
    outcome = reply({"success": True, "data": [1, 2]}, structured=[1, 2])

    assert R.format_problems(outcome, OK, "2025-06-18") == [
        "structuredContent does not carry the same data as the text"
    ]


def test_a_structured_error_that_disagrees_with_the_text_is_a_problem() -> None:
    outcome = reply(
        {"success": False, "error": "Entity x does not exist", "code": "invalid_arguments"},
        structured={"error": {"message": "something else", "code": "tool_error"}},
        is_error=True,
    )

    assert R.format_problems(outcome, FAILS, "2025-06-18") == [
        "structuredContent.error.message 'something else' differs from the text",
        "structuredContent.error.code 'tool_error', expected 'invalid_arguments'",
    ]


def test_the_structured_copy_of_an_unexpected_outcome_is_not_compared() -> None:
    outcome = reply({"success": True, "data": [1]}, structured={"result": [2]})

    assert R.format_problems(outcome, FAILS, "2025-06-18") == [
        "expected a failure with code 'invalid_arguments', the call succeeded"
    ]


def test_structured_content_below_the_boundary_is_a_problem() -> None:
    problems = R.format_problems(success({}, structured=True), OK, "2025-03-26")

    assert problems == ["structuredContent sent to a 2025-03-26 client, which predates it"]


def test_a_resource_link_below_the_boundary_is_a_problem() -> None:
    link: JsonObject = {"type": "resource_link", "uri": "shopware://tool-result/abc", "name": "tool-result"}
    outcome = reply({"success": True, "data": {}}, extra=[link])

    assert R.format_problems(outcome, OK, "2024-11-05") == [
        "resource_link block sent to a 2024-11-05 client, which predates it"
    ]


# --- verify_result_format ------------------------------------------------------


class FakeServer:
    """Answers each probe per revision; records which revisions were opened."""

    def __init__(self, answer: "dict[str, McpResponse] | None" = None, *, refuse: str | None = None) -> None:
        self.answer: dict[str, McpResponse] = answer or {}
        self.refuse: str | None = refuse
        self.opened: list[str] = []
        self.sessions: dict[str, str] = {}

    def init(self, endpoint: Endpoint = ADMIN) -> tuple[str, str]:
        if endpoint.protocol_version == self.refuse:
            raise RuntimeError(f"Asked for protocol {self.refuse}, it negotiated 2024-11-05.")
        self.opened.append(endpoint.protocol_version)
        session = f"s{len(self.opened)}"
        self.sessions[session] = endpoint.protocol_version
        return session, ""

    def call(self, session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        del arguments
        assert self.sessions[session_id] == endpoint.protocol_version, "a probe ran on another revision's session"
        if tool in self.answer:
            return self.answer[tool]
        revision = endpoint.protocol_version
        structured = revision >= R.STRUCTURED_SINCE
        if tool == "shopware-toolsets-list":
            data: JsonObject = {"toolsets": []}
            result: JsonObject = {
                "content": [{"type": "text", "text": json.dumps({"success": True, "data": data})}],
                "isError": False,
                "_meta": STAMP,
            }
            if structured:
                result["structuredContent"] = data
            return response(result)
        envelope: JsonObject = {"success": False, "error": "Entity x does not exist", "code": "invalid_arguments"}
        result = {"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": True, "_meta": STAMP}
        if structured:
            result["structuredContent"] = {"error": {"message": "Entity x does not exist", "code": "invalid_arguments"}}
        return response(result)


def run(monkeypatch: pytest.MonkeyPatch, server: FakeServer, probes: tuple[R.Probe, ...] = (OK, FAILS)) -> Reporter:
    monkeypatch.setattr(R, "mcp_init", server.init)
    monkeypatch.setattr(R, "mcp_call", server.call)
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


def test_a_server_older_than_the_renderer_is_one_skip_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    unstamped: McpResponse = {"result": {"content": [{"type": "text", "text": '{"success": true, "data": {}}'}]}}
    server = FakeServer({"shopware-toolsets-list": unstamped, "shopware-entity-schema": unstamped})
    rep = run(monkeypatch, server)

    assert server.opened == ["2024-11-05"]
    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)
    assert rep.records[0].get("reason") == R.PREDATES_RENDERER
    assert rep.records[0].get("tool") == "check"


def test_a_session_that_will_not_open_fails_that_revision_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(refuse="2025-06-18"))

    labels = {str(r.get("label")): r.get("status") for r in rep.records}
    assert labels["result format 2025-06-18: opens a session"] == "fail"
    assert rep.failed == 1
    assert rep.passed == 4


def test_a_wrong_result_fails_its_probe_with_the_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that forgets structuredContent passes the oldest revision only."""
    plain: McpResponse = {
        "result": {
            "content": [{"type": "text", "text": '{"success": true, "data": {}}'}],
            "isError": False,
            "_meta": STAMP,
        }
    }
    rep = run(monkeypatch, FakeServer({"shopware-toolsets-list": plain}), probes=(OK,))

    assert [r.get("status") for r in rep.records] == ["pass", "fail", "fail"]
    assert "no structuredContent on 2025-06-18" in str(rep.records[1].get("error"))


def test_the_check_skip_counts_and_records_a_reason() -> None:
    rep = Reporter("test", color=False)
    rep.check_skip("thing", "waits for shopware/shopware#1")

    assert rep.skipped == 1
    assert rep.records[0].get("status") == "skipped"
    assert rep.records[0].get("reason") == "waits for shopware/shopware#1"
