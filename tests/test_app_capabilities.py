"""The app capability checks: what they pass, what they report, and that the app goes again.

The MCP replies are the ones a trunk lane and a lane with shopware/shopware#22623
actually sent (see the README). The fixture's real server runs on loopback, and
the fake Admin API registers against it the way Shopware does, so the
registration check sees a real signed request.
"""

import io
import json
import zipfile
from typing import cast, override
from urllib.parse import urlencode

import pytest
import requests

from eval.result_schema import JsonObject, McpResponse
from functional import app_capabilities as A
from functional import app_fixture as F
from functional.known_failures import match
from functional.principals import AdminApi, ProvisioningFailed
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint
from tests.stubs import const, never, raiser

ADMIN: Endpoint = admin_endpoint(base_url="http://shop.example")

TOOL_OK = {"result": {"content": [{"type": "text", "text": ""}], "isError": False}}
PROMPT_OK = {
    "result": {"messages": [{"role": "user", "content": {"type": "text", "text": F.PROMPT_TEXT}}]},
}
PROMPT_TRUNK = {"error": {"code": -32603, "message": "Error while handling prompt"}}
RESOURCE_OK = {
    "result": {"contents": [{"uri": F.RESOURCE_URI, "mimeType": F.RESOURCE_MIME_TYPE, "text": F.RESOURCE_TEXT}]}
}
ENVELOPE = json.dumps({"uri": F.RESOURCE_URI, "mimeType": F.RESOURCE_MIME_TYPE, "text": F.RESOURCE_TEXT})
RESOURCE_TRUNK = {"result": {"contents": [{"uri": F.RESOURCE_URI, "mimeType": F.RESOURCE_MIME_TYPE, "text": ENVELOPE}]}}


def reply(body: object) -> McpResponse:
    return cast(McpResponse, body)


def tool_reply(text: str) -> McpResponse:
    return reply({"result": {"content": [{"type": "text", "text": text}], "isError": False}})


class FakeApi(AdminApi):
    """The extension lifecycle, registering against the fixture's server on upload."""

    def __init__(self, *, fail: str = "", register: bool = True) -> None:  # pyright: ignore[reportMissingSuperCall] -- no token round trip
        self.base: str = "http://shop.example"
        self.token: str = "token"
        self.fail: str = fail
        self.register: bool = register
        self.calls: list[str] = []
        self.archive: bytes = b""

    @override
    def upload(self, path: str, filename: str, content: bytes) -> None:
        self.calls.append(f"UPLOAD {path} {filename}")
        self.archive = content

    @override
    def call(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        self.calls.append(f"{method} {path}")
        if self.fail and f"{method} {path}".startswith(self.fail):
            raise ProvisioningFailed(f"{method} {path} answered HTTP 400: nope")
        if path.endswith(f"/install/app/{F.APP_NAME}") and self.register:
            self._register()
        return {}

    def _register(self) -> None:
        manifest = self.archive_text("manifest.xml")
        base = manifest.split("<registrationUrl>")[1].split("/register<")[0]
        secret = manifest.split("<secret>")[1].split("</secret>")[0]
        query = urlencode({"shop-id": "shop1", "shop-url": "http://shop.example", "timestamp": "1"})
        signature = F.sign(secret, query.encode())
        _ = requests.get(f"{base}/register?{query}", headers={F.APP_SIGNATURE: signature}, timeout=5)

    def archive_text(self, name: str) -> str:
        return zipfile.ZipFile(io.BytesIO(self.archive)).read(f"{F.APP_NAME}/{name}").decode()


class Shop:
    """The MCP side: what each call answers, and whether the shop reached the app."""

    def __init__(
        self,
        *,
        tool: McpResponse | None = None,
        prompt: object = PROMPT_OK,
        resource: object = RESOURCE_OK,
        prompts: list[str] | None = None,
        resources: list[str] | None = None,
        enable: object = TOOL_OK,
    ) -> None:
        self.tool: McpResponse | None = tool
        self.prompt: McpResponse = reply(prompt)
        self.resource: McpResponse = reply(resource)
        self.prompts: list[str] = [F.PROMPT_NAME] if prompts is None else prompts
        self.resources: list[str] = [F.RESOURCE_URI] if resources is None else resources
        self.enable: McpResponse = reply(enable)
        self.sent: list[JsonObject] = []

    def call(self, _session: str, tool: str, arguments: JsonObject, _endpoint: Endpoint) -> McpResponse:
        self.sent.append({"tool": tool, **arguments})
        if self.tool is not None:
            return self.tool
        return tool_reply(json.dumps({"success": True, "data": {"echo": arguments["text"]}}))

    def names(self, _session: str, method: str, _endpoint: Endpoint) -> list[str]:
        return self.prompts if method == "prompts/list" else self.resources

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(A, "mcp_init", const(("session", "")))
        monkeypatch.setattr(A, "enable_toolset", const(self.enable))
        monkeypatch.setattr(A, "mcp_call", self.call)
        monkeypatch.setattr(A, "mcp_list_names", self.names)
        monkeypatch.setattr(A, "mcp_get_prompt", const(self.prompt))
        monkeypatch.setattr(A, "mcp_read_resource", const(self.resource))


def run(monkeypatch: pytest.MonkeyPatch, shop: Shop, api: FakeApi | None = None) -> tuple[Reporter, FakeApi]:
    shop.install(monkeypatch)
    rep = Reporter("srv", color=False, known=())
    fake = api or FakeApi()
    A.verify_app_capabilities(rep, ADMIN, enabled=True, connect=lambda: fake)
    return rep, fake


def outcomes(rep: Reporter) -> dict[str, str]:
    return {str(r["label"]): str(r["status"]) for r in rep.records}


def error_of(rep: Reporter, label: str) -> str:
    return next(str(r.get("error", "")) for r in rep.records if r["label"] == label)


# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------
def test_without_the_flag_nothing_is_installed_and_the_skip_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "AdminApi", never("the shop must not be touched without --app-fixture"))
    rep = Reporter("srv", color=False)

    A.verify_app_capabilities(rep, ADMIN, enabled=False)

    assert rep.records[-1]["status"] == "skipped" and "--app-fixture" in str(rep.records[-1].get("reason"))


def test_no_admin_token_fails_the_install_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "AdminApi", raiser(ProvisioningFailed("no token")))
    rep = Reporter("srv", color=False, known=())

    A.verify_app_capabilities(rep, ADMIN, enabled=True)

    assert outcomes(rep) == {A.INSTALL_CHECK: "fail"}


# ---------------------------------------------------------------------------
# A shop with the fix
# ---------------------------------------------------------------------------
def test_a_fixed_shop_passes_every_check_and_the_app_goes_again(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = Shop()
    rep, api = run(monkeypatch, shop)

    assert set(outcomes(rep).values()) == {"pass"}
    assert list(outcomes(rep)) == [
        A.INSTALL_CHECK,
        A.TOOL_CHECK,
        A.LISTED_CHECK,
        A.PROMPT_CHECK,
        A.RESOURCE_CHECK,
        A.CLEANUP_CHECK,
    ]
    assert api.calls[-2:] == [
        f"POST /api/_action/extension/uninstall/app/{F.APP_NAME}",
        f"POST /api/_action/extension/remove/app/{F.APP_NAME}",
    ]
    assert shop.sent[0]["tool"] == F.TOOL_NAME


def test_a_leftover_is_cleared_before_the_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    _, api = run(monkeypatch, Shop(), FakeApi(fail="POST /api/_action/extension/uninstall"))

    upload = api.calls.index(f"UPLOAD /api/_action/extension/upload {F.APP_NAME}.zip")
    assert api.calls[:upload] == [
        f"POST /api/_action/extension/uninstall/app/{F.APP_NAME}",
        f"POST /api/_action/extension/remove/app/{F.APP_NAME}",
    ]
    assert api.calls[upload + 1 : upload + 3] == [
        f"POST /api/_action/extension/install/app/{F.APP_NAME}",
        f"PUT /api/_action/extension/activate/app/{F.APP_NAME}",
    ]


def test_the_app_points_at_the_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shop reaches the server by the host it is told, not by the bind address."""
    Shop().install(monkeypatch)
    api = FakeApi(register=False)
    rep = Reporter("srv", color=False, known=())

    A.verify_app_capabilities(rep, ADMIN, enabled=True, public_host="192.168.127.254", connect=lambda: api)

    assert "<registrationUrl>http://192.168.127.254:" in api.archive_text("manifest.xml")


# ---------------------------------------------------------------------------
# A trunk shop: the two failures the registry expects
# ---------------------------------------------------------------------------
def test_trunks_prompt_error_is_the_known_one(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(prompt=PROMPT_TRUNK))

    error = error_of(rep, A.PROMPT_CHECK)
    assert error.startswith(f"prompts/get {F.PROMPT_NAME} answered JSON-RPC -32603: Error while handling prompt")
    assert match(A.PROMPT_CHECK, error) is not None


def test_trunks_resource_envelope_is_the_known_one(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(resource=RESOURCE_TRUNK))

    error = error_of(rep, A.RESOURCE_CHECK)
    assert error.startswith(A.WHOLE_ENVELOPE) and ENVELOPE in error
    assert match(A.RESOURCE_CHECK, error) is not None


def test_on_trunk_the_suite_still_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    Shop(prompt=PROMPT_TRUNK, resource=RESOURCE_TRUNK).install(monkeypatch)
    rep = Reporter("srv", color=False)
    fake = FakeApi()

    A.verify_app_capabilities(rep, ADMIN, enabled=True, connect=lambda: fake)

    assert (rep.expected_failed, rep.unexpected_failed, rep.exit_code) == (2, 0, 0)


# ---------------------------------------------------------------------------
# Other failures read as new
# ---------------------------------------------------------------------------
def test_a_different_prompt_error_is_not_the_known_one(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(prompt={"error": {"code": -32602, "message": "Prompt not found"}}))

    assert match(A.PROMPT_CHECK, error_of(rep, A.PROMPT_CHECK)) is None


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "assistant", "content": {"type": "text", "text": F.PROMPT_TEXT}}],
        [{"role": "user", "content": {"type": "text", "text": "other"}}],
    ],
)
def test_a_prompt_that_is_not_the_apps_message_fails(monkeypatch: pytest.MonkeyPatch, messages: list[object]) -> None:
    rep, _ = run(monkeypatch, Shop(prompt={"result": {"messages": messages}}))

    assert error_of(rep, A.PROMPT_CHECK).startswith("expected one user message with the app's text")


def test_a_resource_error_is_reported_with_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(resource={"error": {"code": -32002, "message": "Resource not found"}}))

    assert error_of(rep, A.RESOURCE_CHECK).startswith(f"resources/read {F.RESOURCE_URI} answered JSON-RPC -32002")


@pytest.mark.parametrize(
    "contents",
    [
        [],
        [{"uri": F.RESOURCE_URI, "mimeType": "text/plain", "text": F.RESOURCE_TEXT}],
        [{"uri": F.RESOURCE_URI, "mimeType": F.RESOURCE_MIME_TYPE, "text": "not json"}],
    ],
)
def test_a_resource_that_is_not_the_apps_text_fails_as_new(
    monkeypatch: pytest.MonkeyPatch, contents: list[object]
) -> None:
    rep, _ = run(monkeypatch, Shop(resource={"result": {"contents": contents}}))

    error = error_of(rep, A.RESOURCE_CHECK)
    assert error.startswith("expected application/json text") and match(A.RESOURCE_CHECK, error) is None


def test_a_refused_toolset_fails_the_tool_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(enable={"error": {"code": -32602, "message": "Unknown toolset"}}))

    assert error_of(rep, A.TOOL_CHECK).startswith(f"shopware-toolset-enable {F.APP_NAME} refused")


def test_a_tool_error_reply_fails_the_tool_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(tool=reply({"error": {"code": -32601, "message": "Tool not found"}})))

    assert error_of(rep, A.TOOL_CHECK) == f"tools/call {F.TOOL_NAME} answered JSON-RPC -32601: Tool not found"


def test_a_failed_tool_says_the_shop_never_reached_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = json.dumps({"success": False, "error": "App capability execution failed: cURL error 7"})
    rep, _ = run(monkeypatch, Shop(tool=tool_reply(failed)))

    error = error_of(rep, A.TOOL_CHECK)
    assert "cURL error 7" in error and "never received POST /tool" in error


def test_a_tool_that_answers_something_else_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(tool=tool_reply(json.dumps({"success": True, "data": {"echo": "stale"}}))))

    assert "did not echo" in error_of(rep, A.TOOL_CHECK)


def test_an_unlisted_prompt_and_resource_are_both_named(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(prompts=[], resources=[]))

    assert error_of(rep, A.LISTED_CHECK) == (
        f"prompts/list has no {F.PROMPT_NAME}; resources/list has no {F.RESOURCE_URI}"
    )


def test_a_list_that_errors_fails_the_listed_check_and_the_rest_still_run(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = Shop()
    shop.install(monkeypatch)

    def broken(*_args: object) -> list[str]:
        raise RuntimeError("prompts/list failed: boom")

    monkeypatch.setattr(A, "mcp_list_names", broken)
    rep = Reporter("srv", color=False, known=())
    api = FakeApi()
    A.verify_app_capabilities(rep, ADMIN, enabled=True, connect=lambda: api)

    assert outcomes(rep)[A.LISTED_CHECK] == "fail" and outcomes(rep)[A.RESOURCE_CHECK] == "pass"


# ---------------------------------------------------------------------------
# Install and cleanup
# ---------------------------------------------------------------------------
def test_a_refused_install_is_reported_and_the_app_is_still_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, api = run(monkeypatch, Shop(), FakeApi(fail="POST /api/_action/extension/install"))

    error = error_of(rep, A.INSTALL_CHECK)
    assert "HTTP 400" in error and "never received POST /register" in error
    assert A.TOOL_CHECK not in outcomes(rep)
    assert api.calls[-1] == f"POST /api/_action/extension/remove/app/{F.APP_NAME}"


def test_an_install_without_registration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, Shop(), FakeApi(register=False))

    assert error_of(rep, A.INSTALL_CHECK) == "the app installed without a verified registration request"
    assert outcomes(rep)[A.CLEANUP_CHECK] == "pass"


def test_a_failed_cleanup_says_to_remove_the_app_by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    class StuckApi(FakeApi):
        @override
        def call(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
            if "/remove/" in path and any("activate" in c for c in self.calls):
                raise ProvisioningFailed("remove answered HTTP 500")
            return super().call(method, path, body)

    rep, _ = run(monkeypatch, Shop(), StuckApi())

    assert "Remove McpEvalsFixture by hand" in error_of(rep, A.CLEANUP_CHECK)


def test_a_signature_the_app_rejected_is_named() -> None:
    """The shop reached the app but signed with something else: a different finding."""
    with F.AppServer() as server:
        server.state.record(F.Received("POST", "/prompt", verified=False))
        assert "did not verify" in A._reached(server, "/prompt")
        server.state.record(F.Received("POST", "/tool", verified=True))
        assert A._reached(server, "/tool") == ""


def test_is_envelope_needs_the_apps_text_inside() -> None:
    assert A.is_envelope(ENVELOPE)
    assert not A.is_envelope(F.RESOURCE_TEXT)
    assert not A.is_envelope("not json")
