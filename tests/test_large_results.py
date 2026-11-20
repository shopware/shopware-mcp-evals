"""Large results: the pointer's shape per revision, and who may read it.

Replies are built the way `McpToolResponse::success()` and the renderer send
them since shopware/shopware#20951 (`offloaded`, one format on every revision),
and since shopware/shopware#21844 (`offloaded_by_revision`, the format picked by
the revision).
"""

import json
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from typing import cast, override

import pytest

from eval.result_schema import JsonObject, McpResponse
from functional import large_results as L
from functional.principals import Principal, ProvisioningFailed
from functional.reporting import Reporter
from mcp_client import DEFAULT_PROTOCOL_VERSION, Endpoint, admin_endpoint
from mcp_result import ToolOutcome, read_result

ADMIN: Endpoint = admin_endpoint(base_url="http://shop.example")
TOKEN = "0123456789abcdef0123456789abcdef.1760000000.c2lnbmF0dXJl"
URI = L.TOOL_RESULT_URI_PREFIX + TOKEN
# The pointer a server since #21844 hands a 2025-06-18 client; it stores the plain data.
SPEC_URI = L.TOOL_RESULT_URI_PREFIX + "fedcba9876543210fedcba9876543210.1760000000.c3BlYw"
PLAIN_URI = L.TOOL_RESULT_URI_PREFIX + "0123456789abcdef0123456789abcdef"
EXPIRES = "2026-10-20T11:00:00+00:00"
STAMP: JsonObject = {"shopware/generatedAt": "2026-10-20T10:00:00+00:00"}


def response(result: JsonObject) -> McpResponse:
    return cast(McpResponse, cast(object, {"result": result}))


def error(message: str) -> McpResponse:
    return cast(McpResponse, cast(object, {"error": {"code": -32002, "message": message}}))


def offloaded(revision: str, uri: str = URI, *, block_uri: str | None = None) -> McpResponse:
    """An offloaded reply as the server renders it on `revision`."""
    meta: JsonObject = {"resourceUri": uri, "responseSize": 346116, "note": "Response too large."}
    if uri != PLAIN_URI:
        meta["expiresAt"] = EXPIRES
    content: list[object] = [{"type": "text", "text": json.dumps({"success": True, "data": None, "_meta": meta})}]
    result_meta: JsonObject = dict(STAMP)
    if revision >= L.LINK_SINCE:
        content.append({"type": "resource_link", "uri": block_uri or uri, "name": "tool-result"})
        result_meta["shopware/expiresAt"] = EXPIRES
    return response({"content": content, "isError": False, "_meta": result_meta})


def offloaded_by_revision(revision: str, uri: str = URI) -> McpResponse:
    """An offloaded reply as a server since #21844 renders it on `revision`
    (measured on the trunk lane): the legacy text before 2025-06-18, from it on
    the note, a `{"_meta": …}` block without the pointer, and a `resource_link`."""
    if revision < L.LINK_SINCE:
        return offloaded(revision, uri)
    meta: JsonObject = {"total": 250, "responseSize": 346049}
    content: list[object] = [
        {"type": "text", "text": "Response too large for inline delivery."},
        {"type": "text", "text": json.dumps({"_meta": meta})},
        {"type": "resource_link", "uri": uri, "name": "tool-result", "mimeType": "application/json", "size": 346049},
    ]
    result_meta: JsonObject = {
        **STAMP,
        "shopware/total": 250,
        "shopware/responseSize": 346049,
        "shopware/expiresAt": EXPIRES,
    }
    return response({"content": content, "isError": False, "_meta": result_meta})


def outcome(revision: str, **kwargs: str) -> ToolOutcome:
    return read_result(offloaded(revision, **kwargs))


def stored(rows: list[object], *, plain: bool = False) -> McpResponse:
    text = json.dumps(rows if plain else {"success": True, "data": rows})
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
    assert L.offload_problems(outcome(revision), revision, by_revision=False) == []


@pytest.mark.parametrize("revision", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_a_correct_offload_by_revision_is_clean(revision: str) -> None:
    reply = read_result(offloaded_by_revision(revision))

    assert L.offload_problems(reply, revision) == []


def test_the_pointer_in_the_text_next_to_the_link_is_a_problem_since_21844() -> None:
    """What every 2025-06-18 reply carried before #21844 is wrong after it: the
    `resource_link` is the pointer, and the text is no envelope."""
    assert L.offload_problems(outcome("2025-06-18"), "2025-06-18") == [
        "the text is the legacy format, a 2025-06-18 client gets the MCP format",
        "a resourceUri next to the resource_link: the MCP format links with the block only",
    ]


def test_the_mcp_format_below_the_boundary_is_a_problem_since_21844() -> None:
    reply = read_result(offloaded_by_revision("2025-06-18"))

    assert L.offload_problems(reply, "2024-11-05") == [
        "the text is the spec format, a 2024-11-05 client gets the legacy envelope",
        "no resourceUri next to the pointer in the text",
        "no expiresAt next to the pointer in the text",
        "resource_link block sent to a 2024-11-05 client, which predates it",
    ]


def test_a_link_without_the_block_or_expiry_since_21844_names_both() -> None:
    content: list[object] = [{"type": "text", "text": "Response too large."}]
    meta: JsonObject = {**STAMP, "shopware/resourceUri": URI}
    reply = read_result(response({"content": content, "_meta": meta, "structuredContent": {"result": None}}))

    assert L.offload_problems(reply, "2025-06-18") == [
        "no resource_link block on 2025-06-18",
        "a resourceUri next to the resource_link: the MCP format links with the block only",
        "no shopware/expiresAt in the result _meta on 2025-06-18",
        "structuredContent next to the pointer: a linked result has no data to copy",
    ]


def test_structured_content_next_to_a_legacy_pointer_is_a_problem() -> None:
    meta: JsonObject = {"resourceUri": URI, "expiresAt": EXPIRES}
    text = json.dumps({"success": True, "data": None, "_meta": meta})
    reply = read_result(
        response({"content": [{"type": "text", "text": text}], "_meta": STAMP, "structuredContent": {}})
    )

    assert L.offload_problems(reply, "2024-11-05") == [
        "structuredContent sent to a 2024-11-05 client, which predates it"
    ]


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
    assert L.offload_problems(outcome("2024-11-05", uri=PLAIN_URI), "2024-11-05", by_revision=False) == [
        f"the pointer is not a signed token: {PLAIN_URI}",
        "no expiresAt next to the pointer in the text",
    ]


def test_data_next_to_a_pointer_is_a_problem() -> None:
    meta: JsonObject = {"resourceUri": URI, "expiresAt": EXPIRES}
    text = json.dumps({"success": True, "data": [1], "_meta": meta})
    reply = read_result(response({"content": [{"type": "text", "text": text}], "_meta": STAMP}))

    assert L.offload_problems(reply, "2024-11-05", by_revision=False) == ["data was sent inline next to the pointer"]


def test_from_the_boundary_the_block_and_the_prefixed_expiry_are_required() -> None:
    """The 2024-11-05 rendering, received by a 2025-06-18 client."""
    assert L.offload_problems(outcome("2024-11-05"), "2025-06-18", by_revision=False) == [
        "no resource_link block on 2025-06-18",
        "no shopware/expiresAt in the result _meta on 2025-06-18",
    ]


def test_a_block_naming_another_pointer_is_a_problem() -> None:
    other = L.TOOL_RESULT_URI_PREFIX + "f" * 32 + ".1760000000.b3RoZXI"

    assert L.offload_problems(outcome("2025-06-18", block_uri=other), "2025-06-18", by_revision=False) == [
        "the resource_link block and the text name different pointers"
    ]


def test_a_block_below_the_boundary_is_a_problem() -> None:
    assert L.offload_problems(outcome("2025-06-18"), "2025-03-26", by_revision=False) == [
        "resource_link block sent to a 2025-03-26 client, which predates it"
    ]


# --- resources/read ---------------------------------------------------------------


def test_a_stored_result_with_rows_is_readable() -> None:
    assert L.readable_problem(stored([{"id": "c1"}])) is None


def test_plain_rows_are_readable_when_stored_in_the_mcp_format() -> None:
    assert L.readable_problem(stored([{"id": "c1"}], plain=True), "spec") is None


def test_the_envelope_where_the_plain_data_belongs_is_a_problem() -> None:
    assert L.readable_problem(stored([{"id": "c1"}]), "spec") == (
        "the stored result is the legacy envelope, expected the plain data"
    )


def test_plain_data_where_the_envelope_belongs_is_a_problem() -> None:
    assert L.readable_problem(stored([{"id": "c1"}], plain=True)) == "the stored result is not the legacy envelope"


def test_plain_data_without_rows_is_not_readable() -> None:
    assert L.readable_problem(stored([], plain=True), "spec") == "the stored result carries no rows"


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (NOT_FOUND, "refused: Tool result"),
        (response({"contents": []}), "no text in the resource contents"),
        (response({"contents": [{"text": "<html>"}]}), "the stored result is not JSON: <html>"),
        (stored([]), "the stored result carries no rows"),
    ],
)
def test_anything_else_is_not_readable(reply: McpResponse, problem: str) -> None:
    assert str(L.readable_problem(reply)).startswith(problem)


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

    def __init__(
        self, *, signed: bool = True, leaks: bool = False, refuse: str | None = None, by_revision: bool = False
    ) -> None:
        self.uri: str = URI if signed else PLAIN_URI
        self.by_revision: bool = by_revision
        self.leaks: bool = leaks
        self.refuse: str | None = refuse
        self.sessions: dict[str, str] = {}

    def init(self, endpoint: Endpoint = ADMIN) -> tuple[str, str]:
        if endpoint.protocol_version == self.refuse:
            raise RuntimeError("negotiated something else")
        session = f"s{len(self.sessions)}"
        self.sessions[session] = endpoint.auth_headers["sw-access-key"]
        return session, ""

    def call(self, session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        assert (tool, arguments) == (L.OFFLOAD_TOOL, L.OFFLOAD_ARGS)
        assert session_id in self.sessions
        revision = endpoint.protocol_version
        if self.by_revision:
            return offloaded_by_revision(revision, SPEC_URI if revision >= L.LINK_SINCE else self.uri)
        return offloaded(revision, self.uri)

    def read(self, session_id: str, uri: str, endpoint: Endpoint = ADMIN) -> McpResponse:
        del endpoint
        owner = self.sessions[session_id] == ADMIN.auth_headers["sw-access-key"]
        if not (owner or self.leaks):
            return NOT_FOUND
        if uri == self.uri:
            return stored([{"id": "c1"}])
        # The format follows the call that stored it, not the reader's revision.
        return stored([{"id": "c1"}], plain=True) if self.by_revision and uri == SPEC_URI else NOT_FOUND


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
        "large result 2024-11-05: the same principal reads it on a new session (legacy content)": "pass",
        "large result 2025-06-18: the same principal reads it on a new session (legacy content)": "pass",
        "large result pointer: a later expiry under the same signature is refused": "pass",
        "large result pointer: another principal is refused": "pass",
        "large result pointer: expires after an hour": "skipped",
    }


def test_a_server_picking_the_format_by_revision_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(monkeypatch, FakeServer(by_revision=True))

    assert statuses(rep) == {
        "large result 2024-11-05: stored behind a signed pointer": "pass",
        "large result 2025-06-18: stored behind a signed pointer": "pass",
        "large result 2024-11-05: the same principal reads it on a new session (legacy content)": "pass",
        "large result 2025-06-18: the same principal reads it on a new session (spec content)": "pass",
        "large result pointer: a later expiry under the same signature is refused": "pass",
        "large result pointer: another principal is refused": "pass",
        "large result pointer: expires after an hour": "skipped",
    }


def test_the_formats_swapped_between_the_revisions_fail_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two formats, each on the wrong side of 2025-06-18, is no older server:
    it is held to the per-revision rules, which fail both replies."""
    server = FakeServer(by_revision=True)

    def swapped(session_id: str, tool: str, arguments: JsonObject, endpoint: Endpoint = ADMIN) -> McpResponse:
        del session_id, tool, arguments
        if endpoint.protocol_version < L.LINK_SINCE:
            return offloaded_by_revision("2025-06-18", SPEC_URI)
        return offloaded("2024-11-05")

    monkeypatch.setattr(server, "call", swapped)
    rep = run(monkeypatch, server)

    found = statuses(rep)
    assert found["large result 2024-11-05: stored behind a signed pointer"] == "fail"
    assert found["large result 2025-06-18: stored behind a signed pointer"] == "fail"


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
    assert found["large result 2024-11-05: the same principal reads it on a new session (legacy content)"] == "pass"


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
    L.verify_pointer_binding(rep, ADMIN, [("2025-06-18", URI)], provision=True, by_revision=False)

    assert statuses(rep) == {"large result pointer: opens a session to read it back": "fail"}


def offloaded_spec(revision: str, *, expiry: bool = True) -> McpResponse:
    """The same reply under the `v6.8.0.0` flag (`McpToolResultRenderer::specTexts`):
    no data, the note as the summary, then the metadata as its own block. The
    parser promoted `expiresAt` to the result's expiry, so it is gone from the
    block and sent as `shopware/expiresAt` on every revision."""
    block: JsonObject = {"resourceUri": URI, "responseSize": 346116}
    content: list[object] = [
        {"type": "text", "text": "Response too large."},
        {"type": "text", "text": json.dumps({"_meta": block})},
    ]
    result_meta: JsonObject = {**STAMP, "shopware/resourceUri": URI, "shopware/responseSize": 346116}
    if expiry:
        result_meta["shopware/expiresAt"] = EXPIRES
    if revision >= L.LINK_SINCE:
        content.append({"type": "resource_link", "uri": URI, "name": "tool-result"})
    return response({"content": content, "isError": False, "_meta": result_meta})


@pytest.mark.parametrize("revision", L.OFFLOAD_REVISIONS)
def test_the_spec_format_carries_the_expiry_in_the_result_meta_only(revision: str) -> None:
    """Measured on the next-major lane: the text block has no expiresAt there,
    and asking for one failed both revisions."""
    assert L.offload_problems(read_result(offloaded_spec(revision)), revision, by_revision=False) == []


@pytest.mark.parametrize("revision", L.OFFLOAD_REVISIONS)
def test_the_spec_format_without_an_expiry_is_named(revision: str) -> None:
    problems = L.offload_problems(read_result(offloaded_spec(revision, expiry=False)), revision, by_revision=False)

    assert problems == [f"no shopware/expiresAt in the result _meta on {revision}"]


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


@pytest.mark.parametrize("revision", ["2025-06-18", "2025-11-25"])
def test_a_link_block_without_the_text_pointer_is_a_problem(revision: str) -> None:
    """Core's parser removes only `note` and `expiresAt` from the metadata, so
    the text keeps the pointer on every revision. A renderer that drops it must
    not pass on the link block alone."""
    meta: JsonObject = {"expiresAt": EXPIRES}
    content: list[object] = [
        {"type": "text", "text": json.dumps({"success": True, "data": None, "_meta": meta})},
        {"type": "resource_link", "uri": URI, "name": "tool-result"},
    ]
    result_meta: JsonObject = {**STAMP, "shopware/expiresAt": EXPIRES}
    reply = read_result(response({"content": content, "isError": False, "_meta": result_meta}))

    assert L.offload_problems(reply, revision, by_revision=False) == ["no resourceUri next to the pointer in the text"]
