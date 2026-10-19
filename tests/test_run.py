"""Unit tests for the functional runner: verdict logic, helpers, and the full
admin/store flows driven through a stateful fake MCP server."""

import argparse
import json
from types import SimpleNamespace
from typing import cast, override

import pytest
import requests

import lane
from eval.result_schema import JsonObject, McpResponse, ToolDef, Toolset
from functional import runner as R
from functional.reporting import Reporter
from mcp_client import Endpoint
from tests.stubs import const, raiser

ADMIN = R.endpoint_by_name("admin")
STORE = R.endpoint_by_name("store")


def call_resp(payload: object) -> McpResponse:
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


def flags(*, skip_media_upload: bool, skip_dev_tools: bool) -> argparse.Namespace:
    """The argparse attributes the admin flow reads."""
    return argparse.Namespace(
        endpoint="admin",
        skip_media_upload=skip_media_upload,
        skip_dev_tools=skip_dev_tools,
        provision_principals=False,
    )


def raw_resp(body: JsonObject) -> McpResponse:
    """A reply built key by key, for the malformed shapes the runner has to
    survive — an empty content list, an error with no result."""
    return cast(McpResponse, cast(object, body))


def search_tool(name: str, properties: object = None) -> JsonObject:
    """A tool definition as shopware-tool-search embeds it in its result payload.

    `properties` defaults to an object, matching a spec-conformant server; pass
    `[]` to simulate the empty-array serialization that strict clients reject.
    """
    return {
        "name": name,
        "description": f"{name} description",
        "inputSchema": {"type": "object", "properties": {} if properties is None else properties},
    }


# ---------------------------------------------------------------------------
# _payload
# ---------------------------------------------------------------------------
def test_payload_parses_text_json() -> None:
    assert R._payload(call_resp({"a": 1})) == {"a": 1}


def test_payload_empty_on_garbage() -> None:
    assert R._payload(raw_resp({"result": {"content": [{"text": "not json"}]}})) == {}


def test_payload_empty_on_missing() -> None:
    assert R._payload(raw_resp({})) == {}


# ---------------------------------------------------------------------------
# assert_tool
# ---------------------------------------------------------------------------
def test_assert_tool_pass_on_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(raw_resp({"result": {"content": [{"text": "ok"}]}})))
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert (rep.passed, rep.failed) == (1, 0)


def test_assert_tool_fail_on_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(raw_resp({"error": {"message": "boom"}})))
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert rep.failed == 1


NOTHING_DELETED = {"success": False, "error": 'Nothing was deleted, none of these rows exist: ["' + "0" * 32 + '"]'}


def test_assert_tool_passes_not_found_for_a_phantom_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """shopware-entity-delete's answer to a dry-run delete of ZERO_UUID since
    shopware/shopware#20869. It failed the admin functional suite on trunk."""
    monkeypatch.setattr(R, "mcp_call", const(call_resp(NOTHING_DELETED)))
    rep = Reporter("t", color=False)

    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label", not_found_passes=True)

    assert (rep.passed, rep.failed) == (1, 0)


def test_assert_tool_still_fails_not_found_without_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check that sends a real id must not pass on "not found"."""
    monkeypatch.setattr(R, "mcp_call", const(call_resp(NOTHING_DELETED)))
    rep = Reporter("t", color=False)

    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")

    assert (rep.passed, rep.failed) == (0, 1)


def test_assert_tool_opt_in_does_not_excuse_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(call_resp({"success": False, "error": "Value is not a valid UUID"})))
    rep = Reporter("t", color=False)

    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label", not_found_passes=True)

    assert (rep.passed, rep.failed) == (0, 1)


def test_only_the_phantom_id_delete_check_accepts_not_found() -> None:
    """The opt-in is safe only where the payload names an id that cannot exist."""
    from functional.checks import CORE_CHECKS, ZERO_UUID

    opted_in = [check for check in CORE_CHECKS if check.not_found_passes]

    assert [check.tool for check in opted_in] == ["shopware-entity-delete"]
    assert ZERO_UUID in json.dumps(opted_in[0].args({}))


def test_assert_tool_fail_on_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(raw_resp({"result": {"content": []}})))
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert rep.failed == 1


# ---------------------------------------------------------------------------
# assert_tool_error
# ---------------------------------------------------------------------------
def test_assert_tool_error_pass_with_expected_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(raw_resp({"error": {"message": "Unknown toolset foo"}})))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "Unknown", "label")
    assert (rep.passed, rep.failed) == (1, 0)


def test_assert_tool_error_pass_on_success_false_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(call_resp({"success": False, "message": "bad"})))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "", "label")
    assert rep.passed == 1


def test_assert_tool_error_fail_when_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(call_resp({"success": True})))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "", "label")
    assert rep.failed == 1


def test_assert_tool_error_fail_on_wrong_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(raw_resp({"error": {"message": "some other error"}})))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "Unknown", "label")
    assert rep.failed == 1


# ---------------------------------------------------------------------------
# _first_field
# ---------------------------------------------------------------------------
def test_first_field_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(call_resp({"data": [{"id": "x1"}]})))
    assert R._first_field("s", ADMIN, "product") == "x1"


def test_first_field_empty_when_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "mcp_call", const(call_resp({"data": []})))
    assert R._first_field("s", ADMIN, "product") == ""


# ---------------------------------------------------------------------------
# The media-upload probe: served, or the check skips
# ---------------------------------------------------------------------------
def test_a_served_probe_url_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R.requests, "head", const(SimpleNamespace(status_code=200)))

    assert R._served("http://shop.test/probe.png") == "http://shop.test/probe.png"


def test_a_head_rejecting_server_falls_back_to_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """405 to HEAD is common enough that treating it as absent would skip the
    check on a lane that was seeded correctly."""
    monkeypatch.setattr(R.requests, "head", const(SimpleNamespace(status_code=405)))
    monkeypatch.setattr(R.requests, "get", const(SimpleNamespace(status_code=200)))

    assert R._served("http://shop.test/probe.png") == "http://shop.test/probe.png"


def test_a_missing_probe_is_empty_not_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R.requests, "head", const(SimpleNamespace(status_code=404)))
    monkeypatch.setattr(R.requests, "get", const(SimpleNamespace(status_code=404)))

    assert R._served("http://shop.test/gone.png") == ""


def test_an_unreachable_host_is_empty_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """gather_context runs this before any check; letting it propagate would end
    the run instead of skipping one check."""
    monkeypatch.setattr(R.requests, "head", raiser(requests.exceptions.ConnectionError("refused")))

    assert R._served("http://nowhere.invalid/probe.png") == ""


# ---------------------------------------------------------------------------
# Stateful fake server for the full flows
# ---------------------------------------------------------------------------
ADMIN_TOOLSETS: list[Toolset] = [
    {
        "name": "entity",
        "title": "Entity",
        "description": "DAL",
        "enabled": False,
        "tools": [
            "shopware-entity-read",
            "shopware-entity-search",
            "shopware-entity-schema",
            "shopware-entity-aggregate",
            "shopware-entity-upsert",
            "shopware-entity-delete",
        ],
    },
    {
        "name": "system",
        "title": "System",
        "description": "config",
        "enabled": False,
        "tools": ["shopware-system-config-read", "shopware-system-config-write"],
    },
    {
        "name": "media",
        "title": "Media",
        "description": "media",
        "enabled": False,
        "tools": ["shopware-media-upload", "shopware-theme-config"],
    },
    {
        "name": "order",
        "title": "Order",
        "description": "orders",
        "enabled": False,
        "tools": ["shopware-order-state", "merchant-order-summary"],
    },
    {
        "name": "customer",
        "title": "Customer",
        "description": "customers",
        "enabled": False,
        "tools": ["merchant-customer-lookup", "merchant-checkout-methods"],
    },
    {
        "name": "catalog",
        "title": "Catalog",
        "description": "catalog",
        "enabled": False,
        "tools": ["merchant-storefront-search", "merchant-product-create"],
    },
    {
        "name": "cart",
        "title": "Cart",
        "description": "cart",
        "enabled": False,
        "tools": ["merchant-cart-manage", "merchant-cart-checkout"],
    },
    {
        "name": "reports",
        "title": "Reports",
        "description": "reports",
        "enabled": False,
        "tools": ["merchant-bestseller-report", "merchant-revenue-report"],
    },
]

# The Store endpoint since agentic-commerce 1.3.0 (UCP 2026-08-25): the thirteen
# UCP tools are advertised on a fresh session, and `store-api` is the only
# toolset left. It used to be the opposite — UCP spread across granular cart /
# checkout / catalog toolsets and nothing on the default surface — so these
# fixtures are not a simplification, they are the other shape.
STORE_TOOLSETS: list[Toolset] = [
    {
        "name": "store-api",
        "title": "Store API",
        "description": "store api context",
        "enabled": False,
        "tools": ["shopware-store-api-context", "shopware-store-config-read"],
    },
]


NO_EXTRA: frozenset[str] = frozenset()


class FakeServer:
    def __init__(self, toolsets: list[Toolset], default_extra: frozenset[str] = NO_EXTRA) -> None:
        self.toolsets: list[Toolset] = toolsets
        # Tools this endpoint publishes without anything being enabled, beyond
        # the meta-tools. Empty on admin; the UCP set on store.
        self.default_extra: frozenset[str] = default_extra
        self.names: set[str] = {t["name"] for t in toolsets}
        self.n: int = 0
        self.enabled: dict[str, set[str]] = {}
        self.pinned: dict[str, tuple[str, ...]] = {}
        self.cart_items: list[JsonObject] = []

    def init(self, endpoint: Endpoint | None = None) -> tuple[str, str]:
        # Identity, not `in (ADMIN, STORE)`: `?toolsets=` clones the endpoint, so
        # a pinned connect arrives as a different object against the same path.
        assert endpoint is None or endpoint.path in (ADMIN.path, STORE.path)
        self.n += 1
        sid = f"s{self.n}"
        self.enabled[sid] = set()
        # What the client pinned on the URL. Recorded per session because that is
        # how the server sees it: fixed for the life of the connection, and read
        # on every tools/list rather than only the first.
        self.pinned[sid] = tuple(endpoint.toolsets) if endpoint else ()
        return sid, ""

    def list_toolsets(self, session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
        assert session and (endpoint is None or endpoint.path in (ADMIN.path, STORE.path))
        # `enabled` as a server with shopware/shopware#21772 reports it: enabled
        # on this session, or pinned on its URL.
        pinned = self.pinned.get(session, ())
        on = self.enabled.get(session, set())
        return [
            {**ts, "enabled": R.ALL_TOOLSETS in pinned or ts["name"] in pinned or ts["name"] in on}
            for ts in self.toolsets
        ]

    def enable(self, session: str, toolset: str, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is None or endpoint in (ADMIN, STORE)
        self.enabled.setdefault(session, set()).add(toolset)
        return call_resp({"success": True, "_meta": {"listChanged": True}})

    def tools_list(self, session: str, endpoint: Endpoint | None = None) -> list[ToolDef]:
        assert endpoint is None or endpoint.path in (ADMIN.path, STORE.path)
        names = set(R.META_TOOLS) | self.default_extra
        pinned = self.pinned.get(session, ())
        everything = R.ALL_TOOLSETS in pinned
        for ts in self.toolsets:
            visible = ts["name"] in self.enabled.get(session, set()) or everything or ts["name"] in pinned
            if visible:
                names.update(ts["tools"])
        return [ToolDef(name=n, inputSchema={"type": "object", "properties": {}}) for n in sorted(names)]

    def call(self, session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert session and (endpoint is None or endpoint in (ADMIN, STORE))
        if tool == "shopware-entity-search":
            return call_resp({"data": [{"id": "id-1", "email": "a@b.c"}]})
        if tool == "shopware-toolset-enable":
            if args.get("toolset") not in self.names:
                return call_resp({"success": False, "message": "Unknown toolset"})
            return call_resp({"success": True, "_meta": {"listChanged": True}})
        if tool == "shopware-tool-search":
            query = str(args.get("query", ""))
            count = min(int(cast(int, args.get("maxResults", 5))), 20)
            # Search results carry the full tool definition, inputSchema included —
            # that is what makes a surfaced tool directly callable. Omitting it here
            # would let the schema-conformance check pass against a fake that is
            # laxer than any real server.
            if "cart" in query or "shopping" in query:
                data = [{"tool": search_tool("update_cart"), "score": 0.9, "matchedIn": "desc"}]
            elif "image" in query or "upload" in query:
                data = [{"tool": search_tool("shopware-media-upload"), "score": 0.9, "matchedIn": "desc"}]
            else:
                data = [{"tool": search_tool(f"tool-{i}"), "score": 0.5, "matchedIn": "name"} for i in range(count)]
            return call_resp({"success": True, "data": data, "_meta": {"query": query, "totalCandidates": 20}})
        if tool == "shopware-store-api-context":
            return call_resp({"success": True, "data": {"salesChannelId": "sc1", "token": "tok"}})
        if tool == "merchant-storefront-search":
            return call_resp({"success": True, "data": {"products": [{"id": "p-1"}, {"id": "p-2"}]}})
        if tool == "merchant-cart-manage":
            # Stateful, because the suite's whole claim about carts is that
            # `add` answering `success: true` does not mean anything went in. A
            # fake that returns the same object for create/add/get cannot tell
            # a seeded cart from an empty one, which is the bug it is here to
            # catch.
            action = args.get("action")
            if action == "add":
                self.cart_items.append({"id": "li-1", "referencedId": args.get("productId")})
            if action == "get":
                return call_resp({"success": True, "data": {"lineItems": list(self.cart_items)}})
            return call_resp({"success": True, "data": {"token": "cart-tok"}})
        return call_resp({"success": True, "data": {}})


def _wire(monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> None:
    monkeypatch.setattr(R, "mcp_init", fake.init)
    monkeypatch.setattr(R, "mcp_toolsets_list", fake.list_toolsets)
    monkeypatch.setattr(R, "enable_toolset", fake.enable)
    monkeypatch.setattr(R, "enable_all_toolsets", const(None))
    monkeypatch.setattr(R, "mcp_tools_list_all", fake.tools_list)
    monkeypatch.setattr(R, "mcp_call", fake.call)
    # lane.py holds its own reference: the cart seeding the runner delegates to
    # lives there so the eval suite shares one definition of "a cart that can
    # be checked out". Unpatched, these two tests reached the network.
    monkeypatch.setattr(lane, "mcp_call", fake.call)


def test_run_store_flow_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    import ucp

    fake = FakeServer(STORE_TOOLSETS, default_extra=ucp.all_classified())
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    assert rep.failed == 0
    assert rep.passed >= 10


def test_run_store_flow_when_ucp_is_on_the_default_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces test_run_store_flow_with_granular_ucp_toolsets, whose premise —
    "trunk splits UCP across several granular toolsets" — stopped being true in
    agentic-commerce 1.3.0. The probe for enable/isolation now has to come from
    whichever toolset still defers something, because the UCP ones are gone and
    a probe hardcoded to a UCP tool would never be deferred again."""
    import ucp

    fake = FakeServer(
        [
            {
                "name": "store-api",
                "title": "Store API",
                "description": "context",
                "enabled": False,
                "tools": ["shopware-store-api-context", "shopware-store-config-read"],
            }
        ],
        default_extra=ucp.all_classified(),
    )
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]


def _store_toolsets_with_ucp() -> list[Toolset]:
    import ucp

    return [
        *STORE_TOOLSETS,
        {
            "name": "ucp",
            "title": "UCP",
            "description": "ucp shopping",
            "enabled": False,
            "tools": sorted(ucp.all_classified()),
        },
    ]


def test_run_store_flow_when_ucp_is_its_own_toolset(monkeypatch: pytest.MonkeyPatch) -> None:
    """shopware/agentic-commerce#254: the UCP tools sit in a `ucp` toolset and a
    plain /store-api/_mcp connection advertises only the meta-tools, like admin."""
    fake = FakeServer(_store_toolsets_with_ucp())
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]


def test_run_store_flow_flags_a_ucp_tool_that_is_deferred_and_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UCP tool that is in a toolset must not also sit on the default surface:
    that is a leak, not a published tool."""
    fake = FakeServer(_store_toolsets_with_ucp(), default_extra=frozenset({"search_catalog"}))
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    fails = [r for r in rep.records if r["status"] == "fail"]
    assert any("unexpected tools advertised: search_catalog" in str(r.get("error", "")) for r in fails), fails


def test_run_admin_flow_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    args = flags(skip_media_upload=False, skip_dev_tools=False)
    session, _ = fake.init()
    R.run_admin(rep, ADMIN, args, session)
    assert rep.failed == 0
    assert rep.passed >= 25
    # Named explicitly, because "nothing failed" also holds when the check is
    # SKIPped for want of a cart — which is how it read in CI while the suite
    # looked green here. It must have reached checkout.
    checkout = next(r for r in rep.records if r["tool"] == "merchant-cart-checkout")
    assert checkout["status"] != "skipped", checkout


def test_schema_check_catches_empty_properties_in_the_tool_search_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path that shipped broken three times.

    tools/list is clean here; only the tool-search payload carries
    `"properties": []`. Server-side normalization walks result.tools and never
    reaches this, so a check that only inspects tools/list passes while an
    OpenAI-compatible client rejects the whole request.
    """
    fake = FakeServer(ADMIN_TOOLSETS)
    inner = fake.call

    def call_with_malformed_search(
        session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None
    ) -> McpResponse:
        if tool == "shopware-tool-search":
            return call_resp(
                {
                    "success": True,
                    "data": [{"tool": search_tool("swag-dev-tools-list-skills", properties=[]), "score": 0.9}],
                }
            )
        return inner(session, tool, args, endpoint=endpoint)

    fake.call = call_with_malformed_search
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    session, _ = fake.init()

    R.verify_tool_schemas(rep, session, ADMIN)

    failures = [r for r in rep.records if r["status"] == "fail"]
    assert len(failures) == 1, failures
    assert "tool-search payload" in failures[0]["label"]
    assert "swag-dev-tools-list-skills" in failures[0].get("error", "")


def test_schema_check_passes_when_every_path_is_conformant(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    session, _ = fake.init()

    R.verify_tool_schemas(rep, session, ADMIN)

    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]


def test_run_admin_skips_when_no_seed_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """No products/orders/customers -> the data-dependent asserts skip, not fail."""
    fake = FakeServer(ADMIN_TOOLSETS)
    monkeypatch.setattr(R, "mcp_init", fake.init)

    def call_no_data(_session: str, tool: str, _args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is ADMIN
        if tool == "shopware-entity-search":
            return call_resp({"data": []})
        return call_resp({"success": True, "data": {}})

    monkeypatch.setattr(R, "mcp_call", call_no_data)
    rep = Reporter("admin", color=False)
    args = flags(skip_media_upload=False, skip_dev_tools=False)
    session, _ = fake.init()
    R.run_admin_tools(rep, session, ADMIN, args)
    assert rep.failed == 0
    assert rep.skipped >= 8


def test_run_admin_respects_skip_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    args = flags(skip_media_upload=True, skip_dev_tools=True)
    session, _ = fake.init()
    R.run_admin_tools(rep, session, ADMIN, args)
    labels = " ".join(r["label"] for r in rep.records)
    assert "shopware-media-upload" not in labels
    assert "swag-dev-tools" not in labels


# ---------------------------------------------------------------------------
# Connect-time toolset selection (?toolsets=)
# ---------------------------------------------------------------------------
# The mechanism exists because a client like claude.ai reads tools/list once per
# connection, so shopware-toolset-enable always arrives too late for it. What the
# check has to prove is therefore not "enabling works" but "the catalogue is
# already right on the FIRST enumeration" — which is why the fake resolves pinned
# toolsets per session and none of these call toolset-enable.
CONNECT_TOOLSETS: list[Toolset] = [
    {"name": "order", "title": "Order", "description": "o", "enabled": False, "tools": ["o-1", "o-2"]},
    {"name": "media", "title": "Media", "description": "m", "enabled": False, "tools": ["m-1"]},
    {"name": "theme", "title": "Theme", "description": "t", "enabled": False, "tools": ["t-1"]},
]


def _connect_report(monkeypatch: pytest.MonkeyPatch, fake: FakeServer) -> Reporter:
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    R.verify_connect_time_toolsets(rep, ADMIN)
    return rep


def test_connect_time_toolsets_pass_against_a_server_that_honours_them(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = _connect_report(monkeypatch, FakeServer(CONNECT_TOOLSETS))

    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]
    # one toolset, the union, all, the unknown name, and the plain connect: each
    # once for tools/list and once for what toolsets-list reports enabled
    assert rep.passed == 10


def test_a_server_that_ignores_the_parameter_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression worth catching. Before #20509 the parameter was simply not
    read, and the symptom is silent: a client pins its toolsets, gets the bare
    default surface, and concludes the tools do not exist."""

    class Deaf(FakeServer):
        @override
        def init(self, endpoint: Endpoint | None = None) -> tuple[str, str]:
            sid, instructions = super().init(endpoint)
            self.pinned[sid] = ()  # parameter accepted and discarded
            return sid, instructions

    rep = _connect_report(monkeypatch, Deaf(CONNECT_TOOLSETS))

    # Everything except the plain connect, which is correct either way. A pin
    # that never took effect is not failed a second time for its report.
    assert rep.failed == 4
    assert rep.passed == 2


class ReportsPinsDisabled(FakeServer):
    """The server before shopware/shopware#21772: the pin reaches tools/list,
    and toolsets-list still lists every toolset as disabled."""

    @override
    def list_toolsets(self, session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
        return [{**ts, "enabled": False} for ts in super().list_toolsets(session, endpoint)]


def test_a_pin_listed_as_disabled_is_skipped_naming_the_upstream_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advertising passed this check while toolsets-list told the model to
    enable what it already had. Until the pinned trunk carries the fix, that
    exact symptom is a skip naming the PR, so the nightly stays green and the
    gap stays visible."""
    rep = _connect_report(monkeypatch, ReportsPinsDisabled(CONNECT_TOOLSETS))

    assert (rep.failed, rep.passed, rep.skipped) == (0, 5, 1)
    assert f"SKIP {R.PINNED_REPORTED_DISABLED}" in capsys.readouterr().out
    assert "shopware/shopware#21772" in R.PINNED_REPORTED_DISABLED


def test_a_report_that_names_the_wrong_toolsets_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything other than the known all-disabled symptom is a failure: here
    `all` is reported as only the first toolset, so the model would still go
    and enable the rest."""

    class AllMeansFirst(FakeServer):
        @override
        def list_toolsets(self, session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
            listed = super().list_toolsets(session, endpoint)
            if R.ALL_TOOLSETS in self.pinned.get(session, ()):
                return [{**ts, "enabled": ts["name"] == "media"} for ts in listed]
            return listed

    rep = _connect_report(monkeypatch, AllMeansFirst(CONNECT_TOOLSETS))

    failures = [(r["label"], r.get("error")) for r in rep.records if r["status"] == "fail"]
    assert failures == [
        ("?toolsets=all toolsets-list", "reported enabled: ['media'], pinned: ['media', 'order', 'theme']")
    ]


def test_a_plain_connect_reporting_a_toolset_enabled_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not overshoot: without a pin, nothing is enabled yet. The
    union here is every toolset, so only it and `all` report correctly."""

    class AlwaysEnabled(FakeServer):
        @override
        def list_toolsets(self, session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
            return [{**ts, "enabled": True} for ts in self.toolsets]

    rep = _connect_report(monkeypatch, AlwaysEnabled(CONNECT_TOOLSETS))

    failures = [r["label"] for r in rep.records if r["status"] == "fail"]
    assert failures == [
        "?toolsets=media toolsets-list",
        "?toolsets=no-such-toolset,media toolsets-list",
        "plain connect toolsets-list",
    ]


def test_a_failing_toolsets_list_fails_without_hiding_the_advertising_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(FakeServer):
        @override
        def list_toolsets(self, session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
            if endpoint is not None and endpoint.toolsets:
                raise RuntimeError("shopware-toolsets-list failed: boom")
            return super().list_toolsets(session, endpoint)

    rep = _connect_report(monkeypatch, Broken(CONNECT_TOOLSETS))

    # the four pinned connects fail their report; tools/list passes for all
    # five, and the plain connect's report still passes
    assert rep.failed == 4
    assert rep.passed == 6


def test_an_unknown_toolset_must_not_cost_the_client_the_known_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shop without a plugin is the ordinary case for this: a client pinning
    `order,merchant-catalog` against a shop lacking the merchant plugin has to
    keep `order`, not lose the connection's whole catalogue."""

    class Strict(FakeServer):
        @override
        def init(self, endpoint: Endpoint | None = None) -> tuple[str, str]:
            sid, instructions = super().init(endpoint)
            known = {ts["name"] for ts in self.toolsets} | {R.ALL_TOOLSETS}
            if any(name not in known for name in self.pinned[sid]):
                self.pinned[sid] = ()  # one bad name discards the lot
            return sid, instructions

    rep = _connect_report(monkeypatch, Strict(CONNECT_TOOLSETS))

    failures = [r["label"] for r in rep.records if r["status"] == "fail"]
    assert failures == ["?toolsets with an unknown name"], failures


def test_the_check_skips_where_nothing_is_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint that advertises everything by default has no toolset to pin,
    so there is nothing to assert — and saying so beats a vacuous pass."""
    rep = _connect_report(monkeypatch, FakeServer([]))

    assert (rep.failed, rep.passed, rep.skipped) == (0, 0, 1)
