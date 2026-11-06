"""The fixture app: its zip, and the server that speaks the app protocol.

The server runs for real on loopback here. What matters is that it answers only
what Shopware signed, that registration proves what Shopware checks, and that
each capability answers in the shape the app docs promise.
"""

import io
import json
import zipfile
from collections.abc import Generator
from typing import cast
from urllib.parse import urlencode, urlsplit

import pytest
import requests

from eval.result_schema import JsonObject, as_object
from functional import app_fixture as F


@pytest.fixture
def server() -> Generator[F.AppServer]:
    with F.AppServer() as app:
        yield app


def post(server: F.AppServer, path: str, body: object, secret: str | None = None) -> requests.Response:
    raw = json.dumps(body).encode()
    signature = F.sign(server.state.app_secret if secret is None else secret, raw)
    return requests.post(
        server.base_url("127.0.0.1") + path, data=raw, headers={F.SHOP_SIGNATURE: signature}, timeout=5
    )


def json_of(response: requests.Response) -> object:
    return cast(object, response.json())


# ---------------------------------------------------------------------------
# The zip
# ---------------------------------------------------------------------------
def test_the_zip_has_one_root_named_after_the_app() -> None:
    archive = zipfile.ZipFile(io.BytesIO(F.render_app("http://10.0.0.1:1234", "s3cret")))

    assert sorted(archive.namelist()) == ["McpEvalsFixture/Resources/mcp.xml", "McpEvalsFixture/manifest.xml"]


def test_the_placeholders_are_filled_in() -> None:
    archive = zipfile.ZipFile(io.BytesIO(F.render_app("http://10.0.0.1:1234", "s3cret")))
    manifest = archive.read("McpEvalsFixture/manifest.xml").decode()
    mcp = archive.read("McpEvalsFixture/Resources/mcp.xml").decode()

    assert "<registrationUrl>http://10.0.0.1:1234/register</registrationUrl>" in manifest
    assert "<secret>s3cret</secret>" in manifest
    assert 'url="http://10.0.0.1:1234/tool"' in mcp and 'uri="mcp-evals-fixture://status"' in mcp
    assert "{{" not in manifest + mcp


def test_the_capability_names_match_the_manifest() -> None:
    """The check calls them by the name Shopware derives: `<app>-<name>`."""
    mcp = (F.TEMPLATE / "Resources" / "mcp.xml").read_text()
    assert f'name="{F.TOOL_NAME.removeprefix(F.APP_NAME + "-")}"' in mcp
    assert f'name="{F.PROMPT_NAME.removeprefix(F.APP_NAME + "-")}"' in mcp
    assert f'mime-type="{F.RESOURCE_MIME_TYPE}"' in mcp


def test_each_run_mints_its_own_secrets() -> None:
    first, second = F.AppState(), F.AppState()
    assert first.manifest_secret != second.manifest_secret
    assert first.app_secret != first.manifest_secret


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_registration_answers_the_proof_shopware_checks(server: F.AppServer) -> None:
    query = urlencode({"shop-id": "shop1", "shop-url": "http://shop.example", "timestamp": "1"})
    signature = F.sign(server.state.manifest_secret, query.encode())

    response = requests.get(
        f"{server.base_url('127.0.0.1')}/register?{query}", headers={F.APP_SIGNATURE: signature}, timeout=5
    )

    body = as_object(json_of(response))
    assert response.status_code == 200
    assert body["proof"] == F.sign(server.state.manifest_secret, b"shop1http://shop.exampleMcpEvalsFixture")
    assert body["secret"] == server.state.app_secret
    assert urlsplit(str(body["confirmation_url"])).path == "/confirm"
    assert [(e.path, e.verified) for e in server.state.seen("/register")] == [("/register", True)]


def test_registration_with_a_wrong_signature_is_refused(server: F.AppServer) -> None:
    response = requests.get(
        f"{server.base_url('127.0.0.1')}/register?shop-id=x", headers={F.APP_SIGNATURE: "nope"}, timeout=5
    )

    assert response.status_code == 401
    assert [e.verified for e in server.state.seen("/register")] == [False]


def test_any_other_get_is_not_found(server: F.AppServer) -> None:
    assert requests.get(server.base_url("127.0.0.1") + "/elsewhere", timeout=5).status_code == 404


def test_the_confirmation_is_accepted_when_signed(server: F.AppServer) -> None:
    response = post(server, "/confirm", {"apiKey": "k", "secretKey": "s"})

    assert response.status_code == 200 and json_of(response) == {"success": True}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
def test_the_tool_echoes_its_text(server: F.AppServer) -> None:
    response = post(server, "/tool", {"tool": F.TOOL_NAME, "arguments": {"text": "nonce"}})

    assert json_of(response) == {"success": True, "data": {"echo": "nonce"}}
    [seen] = server.state.seen("/tool")
    assert seen.verified and as_object(seen.body)["tool"] == F.TOOL_NAME


def test_the_prompt_is_one_user_message(server: F.AppServer) -> None:
    assert json_of(post(server, "/prompt", {"arguments": {}})) == [{"role": "user", "content": F.PROMPT_TEXT}]


def test_the_resource_answers_uri_mime_type_and_text(server: F.AppServer) -> None:
    body = as_object(json_of(post(server, "/resource", {"arguments": {"uri": F.RESOURCE_URI}})))

    assert body == {"uri": F.RESOURCE_URI, "mimeType": F.RESOURCE_MIME_TYPE, "text": F.RESOURCE_TEXT}


def test_a_resource_request_without_arguments_still_names_the_uri(server: F.AppServer) -> None:
    body: JsonObject = as_object(json_of(post(server, "/resource", {"arguments": "not a map"})))
    assert body["uri"] == F.RESOURCE_URI


def test_a_body_that_is_not_json_is_recorded_without_one(server: F.AppServer) -> None:
    raw = b"not json"
    response = requests.post(
        server.base_url("127.0.0.1") + "/tool",
        data=raw,
        headers={F.SHOP_SIGNATURE: F.sign(server.state.app_secret, raw)},
        timeout=5,
    )

    assert json_of(response) == {"success": True, "data": {"echo": None}}
    assert server.state.seen("/tool")[0].body is None


def test_an_unsigned_call_is_refused(server: F.AppServer) -> None:
    response = post(server, "/tool", {"arguments": {"text": "x"}}, secret="wrong")

    assert response.status_code == 401
    assert json_of(response) == {"success": False, "error": "bad signature"}


def test_an_unknown_post_is_not_found(server: F.AppServer) -> None:
    assert post(server, "/elsewhere", {}).status_code == 404


def test_the_base_url_names_the_public_host_and_the_bound_port(server: F.AppServer) -> None:
    assert server.base_url("192.168.127.254") == f"http://192.168.127.254:{server.port}"
