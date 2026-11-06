"""The tool annotation checks, against a faked tools/list and tool-search."""

import json
from typing import cast

import pytest

from eval.result_schema import JsonObject, McpResponse
from functional import tool_annotations as T
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint, store_endpoint
from tests.stubs import const

ADMIN: Endpoint = admin_endpoint(access_key="SWUA", secret_access_key="secret", base_url="http://shop.example")
STORE: Endpoint = store_endpoint(access_key="SWSC", context_token="ctx", base_url="http://shop.example")


def listed(expected: dict[str, JsonObject], drop: str = "", override: JsonObject | None = None) -> list[JsonObject]:
    tools: list[JsonObject] = []
    for name, hints in expected.items():
        if name == drop:
            continue
        tools.append({"name": name, "annotations": override if override is not None else dict(hints)})
    return tools


def search_reply(*tools: JsonObject, error: bool = False) -> McpResponse:
    envelope: JsonObject = {"success": not error, "data": [{"tool": t, "score": 1.0, "matchedIn": []} for t in tools]}
    if error:
        envelope = {"success": False, "error": "boom"}
    result: JsonObject = {"content": [{"type": "text", "text": json.dumps(envelope)}]}
    if error:
        result["isError"] = True
    return cast(McpResponse, cast(object, {"jsonrpc": "2.0", "id": 1, "result": result}))


DELETE_TOOL: JsonObject = {"name": T.SEARCHED_TOOL, "annotations": T.ADMIN_EXPECTED[T.SEARCHED_TOOL]}


def stub(monkeypatch: pytest.MonkeyPatch, tools: list[JsonObject], search: McpResponse) -> None:
    monkeypatch.setattr(T, "mcp_init", const(("session", "")))
    monkeypatch.setattr(T, "enable_all_toolsets", const([]))
    monkeypatch.setattr(T, "mcp_tools_list_all", const(tools))
    monkeypatch.setattr(T, "mcp_call", const(search))


def statuses(rep: Reporter) -> dict[str, str]:
    return {r["label"]: r["status"] for r in rep.records}


def reporter() -> Reporter:
    return Reporter("http://shop.example", color=False)


def test_every_admin_tool_with_its_hints_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, listed(T.ADMIN_EXPECTED), search_reply(DELETE_TOOL))
    rep = reporter()

    T.verify_tool_annotations(rep, ADMIN)

    assert rep.failed == 0, rep.records
    assert len(rep.records) == len(T.ADMIN_EXPECTED) + 1
    assert statuses(rep)[f"tool annotations: tool-search returns the hints of {T.SEARCHED_TOOL} (admin)"] == "pass"


def test_a_server_without_annotations_is_the_skip_naming_the_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, [{"name": n} for n in T.ADMIN_EXPECTED], search_reply(DELETE_TOOL))
    rep = reporter()

    T.verify_tool_annotations(rep, ADMIN)

    assert [(r["label"], r["status"]) for r in rep.records] == [("tool annotations (admin)", "skipped")]
    assert "#22058" in rep.records[0].get("reason", "")


def test_a_wrong_hint_fails_and_a_missing_tool_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = listed(T.ADMIN_EXPECTED, drop="shopware-theme-config")
    tools[0]["annotations"] = {"readOnlyHint": False}
    stub(monkeypatch, tools, search_reply(DELETE_TOOL))
    rep = reporter()

    T.verify_tool_annotations(rep, ADMIN)

    status = statuses(rep)
    assert status["tool annotations: shopware-entity-schema declares its hints in tools/list (admin)"] == "fail"
    assert status["tool annotations: shopware-theme-config declares its hints in tools/list (admin)"] == "skipped"


@pytest.mark.parametrize(
    ("search", "problem"),
    [
        pytest.param(search_reply(), "not listed", id="no-hit"),
        pytest.param(search_reply({"name": T.SEARCHED_TOOL}), "no annotations", id="bare-definition"),
        pytest.param(search_reply(error=True), "shopware-tool-search failed", id="error"),
    ],
)
def test_the_search_check_names_what_is_wrong(
    monkeypatch: pytest.MonkeyPatch, search: McpResponse, problem: str
) -> None:
    stub(monkeypatch, listed(T.ADMIN_EXPECTED), search)
    rep = reporter()

    T.verify_tool_annotations(rep, ADMIN)

    record = next(r for r in rep.records if "tool-search returns" in r["label"])
    assert record["status"] == "fail"
    assert problem in record.get("error", "")


def test_the_store_checks_its_own_tools_and_does_not_search(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, listed(T.STORE_EXPECTED), search_reply())
    rep = reporter()

    T.verify_tool_annotations(rep, STORE)

    assert rep.failed == 0, rep.records
    assert {r["label"] for r in rep.records} == {
        f"tool annotations: {name} declares its hints in tools/list (store)" for name in T.STORE_EXPECTED
    }


def test_an_extra_hint_the_expectation_leaves_out_is_a_problem() -> None:
    tool: JsonObject = {"name": "x", "annotations": {**T.READ_ONLY, "destructiveHint": False, "title": "X"}}

    assert "declares" in (T.annotation_problem(tool, T.READ_ONLY) or "")
