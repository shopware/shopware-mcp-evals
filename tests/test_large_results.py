"""Large results: the pointer's shape per revision, and who may read it.

Replies are built the way `McpToolResultRenderer::linkedResult()` sends them
since shopware/shopware#21844: the legacy text with the pointer below
2025-06-18, a `resource_link` block and no pointer in the text from it on.
"""

import json
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from typing import cast, override

import pytest

from eval.result_schema import JsonObject, McpResponse, as_list, as_object
from functional import large_results as L
from functional.principals import Principal, ProvisioningFailed
from functional.reporting import Reporter
from mcp_client import DEFAULT_PROTOCOL_VERSION, Endpoint, admin_endpoint
from mcp_result import ToolOutcome, read_result

ADMIN: Endpoint = admin_endpoint(base_url="http://shop.example")
TOKEN = "0123456789abcdef0123456789abcdef.1760000000.c2lnbmF0dXJl"
URI = L.TOOL_RESULT_URI_PREFIX + TOKEN
PLAIN_URI = L.TOOL_RESULT_URI_PREFIX + "0123456789abcdef0123456789abcdef"
EXPIRES = "2026-10-20T11:00:00+00:00"
STAMP: JsonObject = {"shopware/generatedAt": "2026-10-20T10:00:00+00:00"}


def response(result: JsonObject) -> McpResponse:
    return cast(McpResponse, cast(object, {"result": result}))


def error(message: str) -> McpResponse:
    return cast(McpResponse, cast(object, {"error": {"code": -32002, "message": message}}))


NOTE = "Response too large for inline delivery."


def offloaded(revision: str, uri: str = URI, *, text_uri: bool = False, expiry: bool = True) -> McpResponse:
    """An offloaded reply as the server renders it on `revision`.

    `text_uri` adds the pointer to the text on the MCP format too, as the
    renderer did before #21844; `expiry` drops the expiry on either format."""
    result_meta: JsonObject = dict(STAMP)
    if expiry and uri != PLAIN_URI:
        result_meta["shopware/expiresAt"] = EXPIRES
    if revision < L.LINK_SINCE:
        meta: JsonObject = {"responseSize": 346116, "resourceUri": uri, "note": NOTE}
        if expiry and uri != PLAIN_URI:
            meta["expiresAt"] = EXPIRES
        text = json.dumps({"success": True, "data": None, "_meta": meta})
        return response({"content": [{"type": "text", "text": text}], "isError": False, "_meta": result_meta})
    block: JsonObject = {"responseSize": 346116, "query": {"tool": L.OFFLOAD_TOOL}}
    if text_uri:
        block["resourceUri"] = uri
    content: list[object] = [
        {"type": "text", "text": NOTE},
        {"type": "text", "text": json.dumps({"_meta": block})},
        {"type": "resource_link", "uri": uri, "name": "tool-result", "mimeType": "application/json", "size": 346116},
    ]
    result_meta.update({f"shopware/{k}": v for k, v in block.items()})
    return response({"content": content, "isError": False, "_meta": result_meta})


def outcome(revision: str, uri: str = URI, *, text_uri: bool = False, expiry: bool = True) -> ToolOutcome:
    return read_result(offloaded(revision, uri, text_uri=text_uri, expiry=expiry))


def stored(rows: list[object], revision: str = "2024-11-05") -> McpResponse:
    """resources/read of a result stored on `revision`: the legacy envelope
    below the boundary, the plain data from it on."""
    text = json.dumps(rows if revision >= L.LINK_SINCE else {"success": True, "data": rows})
    return response({"contents": [{"uri": URI, "mimeType": "application/json", "text": text}]})


NOT_FOUND = error(f'Tool result "{TOKEN}" not found. It may have expired or been stored by another caller.')


# --- tokens ---------------------------------------------------------------------


def test_a_signed_token_is_recognised() -> None:
    assert L.is_signed(URI)


@pytest.mark.parametrize("uri", [PLAIN_URI, L.TOOL_RESULT_URI_PREFIX + "xyz.1.sig", L.TOOL_RESULT_URI_PREFIX + "a.b"])
def test_anything_else_is_not_a_signed_token(uri: str) -> None:
    assert not L.is_signed(uri)


def test_tampering_moves_the_expiry_and_keeps_the_signature() -> None:
    assert L.tampered(URI) == L.TOOL_RESULT_URI_PREFIX + "0123456789abcdef0123456789abcdef.1760003600.c2lnbmF0dXJl"


# --- offload_problems --------------------------------------------------------------


@pytest.mark.parametrize("revision", ["2024-11-05", "2025-06-18", "2025-11-25"])
def test_a_correct_offload_is_clean(revision: str) -> None:
    assert L.offload_problems(outcome(revision), revision) == []


def test_a_failed_call_is_reported_alone() -> None:
    failed = read_result(error("Tool is not enabled in your MCP allowlist."))

    assert L.offload_problems(failed, "2024-11-05") == ["the call failed: Tool is not enabled in your MCP allowlist."]


def test_an_inline_result_says_the_probe_shrank() -> None:
    inline = read_result(
        response({"content": [{"type": "text", "text": '{"success":true,"data":[]}'}], "_meta": STAMP})
    )

    problems = L.offload_problems(inline, "2024-11-05")

    assert len(problems) == 1
    assert "the probe no longer exceeds 100 KB" in problems[0]


def test_a_plain_id_without_expiry_is_two_problems() -> None:
    assert L.offload_problems(outcome("2024-11-05", uri=PLAIN_URI), "2024-11-05") == [
        f"the pointer is not a signed token: {PLAIN_URI}",
        "no expiresAt next to the pointer in the text",
    ]


def test_data_next_to_a_pointer_is_a_problem() -> None:
    meta: JsonObject = {"resourceUri": URI, "expiresAt": EXPIRES}
    text = json.dumps({"success": True, "data": [1], "_meta": meta})
    reply = read_result(response({"content": [{"type": "text", "text": text}], "_meta": STAMP}))

    assert L.offload_problems(reply, "2024-11-05") == ["data was sent inline next to the pointer"]


def test_a_reply_from_the_boundary_carries_no_structured_content_and_no_data() -> None:
    """Pins the builder to the renderer: a linked result has no data, so
    `structuredContent` is null and the first text is the note."""
    reply = outcome("2025-06-18")

    assert (reply.shape, reply.data, reply.has_structured, reply.summary) == ("spec", None, False, NOTE)
    assert [(link.uri, link.source) for link in reply.links] == [(URI, "resource_link")]


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_the_legacy_rendering_from_the_boundary_is_two_problems(revision: str) -> None:
    """The 2024-11-05 rendering, received by a client that gets the MCP format."""
    assert L.offload_problems(outcome("2024-11-05"), revision) == [
        f"no resource_link block on {revision}",
        f"resourceUri in the text on {revision}, where the resource_link block is the pointer",
    ]


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_a_pointer_in_the_text_next_to_the_block_is_a_problem(revision: str) -> None:
    """What the renderer sent before #21844: both, naming the same pointer."""
    assert L.offload_problems(outcome(revision, text_uri=True), revision) == [
        f"resourceUri in the text on {revision}, where the resource_link block is the pointer"
    ]


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_from_the_boundary_the_prefixed_expiry_is_required(revision: str) -> None:
    assert L.offload_problems(outcome(revision, expiry=False), revision) == [
        f"no shopware/expiresAt in the result _meta on {revision}"
    ]


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_from_the_boundary_structured_content_next_to_the_pointer_is_a_problem(revision: str) -> None:
    """The data is the stored resource, so the renderer sends none."""
    raw = as_object(offloaded(revision).get("result"))
    raw["structuredContent"] = {"result": None, "_meta": {"query": {"tool": L.OFFLOAD_TOOL}}}

    assert L.offload_problems(read_result(response(raw)), revision) == [
        f"structuredContent sent with the pointer on {revision}"
    ]


def test_below_the_boundary_the_text_expiry_is_required() -> None:
    assert L.offload_problems(outcome("2024-11-05", expiry=False), "2024-11-05") == [
        "no expiresAt next to the pointer in the text"
    ]


def test_the_mcp_rendering_below_the_boundary_is_named() -> None:
    """What the `v6.8.0.0` flag sent a 2024-11-05 client before #21844."""
    assert L.offload_problems(outcome("2025-06-18"), "2024-11-05") == [
        "no resourceUri next to the pointer in the text",
        "no expiresAt next to the pointer in the text",
        "resource_link block sent to a 2024-11-05 client, which predates it",
    ]


def test_a_block_below_the_boundary_is_a_problem() -> None:
    raw = as_object(offloaded("2024-11-05").get("result"))
    content = as_list(raw.get("content"))
    content.append({"type": "resource_link", "uri": URI, "name": "tool-result"})

    assert L.offload_problems(read_result(response(raw)), "2025-03-26") == [
        "resource_link block sent to a 2025-03-26 client, which predates it"
    ]


def test_a_link_of_the_tools_own_is_not_the_pointer() -> None:
    """The renderer keeps a tool's own links in front of the pointer's."""
    raw = as_object(offloaded("2025-06-18").get("result"))
    content = as_list(raw.get("content"))
    content.insert(2, {"type": "resource_link", "uri": "https://shop.example/media/1.png", "name": "image"})
    reply = read_result(response(raw))

    assert L.pointer(reply) == next(link for link in reply.links if link.uri == URI)
    assert L.offload_problems(reply, "2025-06-18") == []


# --- resources/read ---------------------------------------------------------------


@pytest.mark.parametrize("revision", ["2024-11-05", "2025-06-18", "2025-11-25"])
def test_a_stored_result_with_rows_is_readable_in_the_form_of_its_revision(revision: str) -> None:
    assert L.readable_problem(stored([{"id": "c1"}], revision), revision) is None


def test_an_envelope_stored_from_the_boundary_is_a_problem() -> None:
    assert L.readable_problem(stored([{"id": "c1"}], "2024-11-05"), "2025-06-18") == (
        "the stored result is the legacy envelope, stored on 2025-06-18, which stores the plain data"
    )


def test_plain_data_stored_below_the_boundary_carries_no_rows_where_the_envelope_has_them() -> None:
    assert L.readable_problem(stored([{"id": "c1"}], "2025-06-18"), "2024-11-05") == "the stored result carries no rows"


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (NOT_FOUND, "refused: Tool result"),
        (response({"contents": []}), "no text in the resource contents"),
        (response({"contents": [{"text": "<html>"}]}), "the stored result is not JSON: <html>"),
    ],
)
@pytest.mark.parametrize("revision", ["2024-11-05", "2025-06-18"])
def test_anything_else_is_not_readable(reply: McpResponse, problem: str, revision: str) -> None:
    assert str(L.readable_problem(reply, revision)).startswith(problem)


@pytest.mark.parametrize("revision", ["2024-11-05", "2025-06-18"])
def test_a_stored_result_without_rows_is_not_readable(revision: str) -> None:
    assert L.readable_problem(stored([], revision), revision) == "the stored result carries no rows"


def test_the_not_found_error_is_a_refusal() -> None:
    assert L.refused_problem(NOT_FOUND) is None


def test_returning_the_result_is_not_a_refusal() -> None:
    assert L.refused_problem(stored([1])) == "the stored result was returned"


def test_an_unrelated_error_is_not_a_refusal() -> None:
    """ "Method not found" contains "not found" and proves nothing about ownership."""
    assert L.refused_problem(error("Method not found")) == "refused, but not as not-found: Method not found"


# --- verify_large_results -------------------------------------------------------------


class FakeServer:
    """One stored result, readable by the suite's principal only."""

    def __init__(self, *, signed: bool = True, leaks: bool = False, refuse: str | None = None) -> None:
        self.uri: str = URI if signed else PLAIN_URI
        self.leaks: bool = leaks
        self.refuse: str | None = refuse
        self.sessions: dict[str, str] = {}
        self.stored_on: str = "2024-11-05"

    def init(self, endpoint: Endpoint = ADMIN) -> tuple[str, str]:
        if endpoint.protocol_version == self.refuse:
            raise RuntimeError("negotiated something else")
        session = f"s{len(self.sessions)}"
        self.sessions[session] = endpoint.auth_headers["sw-access-key"]
        return session, ""

    def call(self, session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        assert (tool, arguments) == (L.OFFLOAD_TOOL, L.OFFLOAD_ARGS)
        assert session_id in self.sessions
        self.stored_on = endpoint.protocol_version
        return offloaded(endpoint.protocol_version, self.uri)

    def read(self, session_id: str, uri: str, endpoint: Endpoint = ADMIN) -> McpResponse:
        del endpoint
        owner = self.sessions[session_id] == ADMIN.auth_headers["sw-access-key"]
        return stored([{"id": "c1"}], self.stored_on) if uri == self.uri and (owner or self.leaks) else NOT_FOUND


@contextmanager
def other_principal(_api: object) -> Generator[Principal]:
    yield Principal("other", "SWIAOTHER", "secret", "all")


def no_api(*_args: object) -> object:
    return object()


class Broken(AbstractContextManager[Principal]):
    """A second principal the Admin API refused to create."""

    @override
    def __enter__(self) -> Principal:  # pyright: ignore[reportMissingSuperCall] -- refuses, as the API did
        raise ProvisioningFailed("HTTP 400")

    @override
    def __exit__(self, *_exc: object) -> None:
        return None


def run(
    monkeypatch: pytest.MonkeyPatch,
    server: FakeServer,
    *,
    provision: bool = True,
    principal: Callable[[object], AbstractContextManager[Principal]] = other_principal,
) -> Reporter:
    monkeypatch.setattr(L, "mcp_init", server.init)
    monkeypatch.setattr(L, "mcp_call", server.call)
    monkeypatch.setattr(L, "mcp_read_resource", server.read)
    monkeypatch.setattr(L, "AdminApi", no_api)
    monkeypatch.setattr(L, "another_principal", principal)
    rep = Reporter("test", color=False)
    L.verify_large_results(rep, ADMIN, provision=provision)
    return rep


def statuses(rep: Reporter) -> dict[str, str]:
    return {str(r.get("label")): str(r.get("status")) for r in rep.records}


def test_a_correct_server_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer())

    assert statuses(rep) == {
        "large result 2024-11-05: stored behind a signed pointer": "pass",
        "large result 2025-06-18: stored behind a signed pointer": "pass",
        "large result pointer: the same principal reads it on a new session": "pass",
        "large result pointer: a later expiry under the same signature is refused": "pass",
        "large result pointer: another principal is refused": "pass",
        "large result pointer: expires after an hour": "skipped",
    }


def test_a_pointer_another_principal_can_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(leaks=True))

    assert statuses(rep)["large result pointer: another principal is refused"] == "fail"


def test_a_server_older_than_signed_pointers_is_one_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(signed=False))

    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)
    assert rep.records[0].get("reason") == L.PREDATES_SIGNED


def test_without_provisioning_the_second_principal_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(), provision=False)

    record = next(r for r in rep.records if r.get("label") == "large result pointer: another principal is refused")
    assert (record.get("status"), record.get("reason")) == ("skipped", L.NEEDS_PROVISIONING)


def test_a_provisioning_failure_fails_that_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(), principal=lambda _api: Broken())

    record = next(r for r in rep.records if r.get("label") == "large result pointer: another principal is refused")
    assert record.get("status") == "fail"
    assert "HTTP 400" in str(record.get("error"))


def test_a_revision_that_will_not_open_still_tries_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(refuse="2025-06-18"))

    found = statuses(rep)
    assert found["large result 2025-06-18: opens a session"] == "fail"
    assert found["large result pointer: the same principal reads it on a new session"] == "pass"


def test_no_good_pointer_means_no_binding_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer()

    def refused(*_args: object, **_kwargs: object) -> McpResponse:
        return error("Tool is not enabled in your MCP allowlist.")

    monkeypatch.setattr(server, "call", refused)
    rep = run(monkeypatch, server)

    assert [r.get("status") for r in rep.records] == ["fail", "fail", "skipped"]


def test_a_session_that_will_not_open_for_the_read_back_fails_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's own endpoint asks for the default revision."""
    server = FakeServer()
    rep = run(monkeypatch, server)
    server.refuse = DEFAULT_PROTOCOL_VERSION
    rep = Reporter("test", color=False)
    L.verify_pointer_binding(rep, ADMIN, URI, provision=True)

    assert statuses(rep) == {"large result pointer: opens a session to read it back": "fail"}


def test_the_second_principal_reads_on_the_suites_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The offload pins each revision itself; everything after it, including the
    rebuilt endpoint of the second principal, stays on the caller's revision."""
    server = FakeServer()
    seen: list[str] = []
    inner = server.init

    def init(endpoint: Endpoint = ADMIN) -> tuple[str, str]:
        seen.append(endpoint.protocol_version)
        return inner(endpoint)

    monkeypatch.setattr(L, "mcp_init", init)
    monkeypatch.setattr(L, "mcp_call", server.call)
    monkeypatch.setattr(L, "mcp_read_resource", server.read)
    monkeypatch.setattr(L, "AdminApi", no_api)
    monkeypatch.setattr(L, "another_principal", other_principal)
    rep = Reporter("test", color=False)

    L.verify_large_results(rep, ADMIN.with_protocol("2025-06-18"), provision=True)

    assert rep.failed == 0
    assert seen == ["2024-11-05", "2025-06-18", "2025-06-18", "2025-06-18"]
