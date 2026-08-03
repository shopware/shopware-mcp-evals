"""Unit tests for the functional runner: verdict logic, helpers, and the full
admin/store flows driven through a stateful fake MCP server."""

import json
from types import SimpleNamespace

import lane
from functional import runner as R
from functional.reporting import Reporter

ADMIN = R.endpoint_by_name("admin")
STORE = R.endpoint_by_name("store")


def call_resp(payload):
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


def search_tool(name, properties=None):
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
def test_payload_parses_text_json():
    assert R._payload(call_resp({"a": 1})) == {"a": 1}


def test_payload_empty_on_garbage():
    assert R._payload({"result": {"content": [{"text": "not json"}]}}) == {}


def test_payload_empty_on_missing():
    assert R._payload({}) == {}


# ---------------------------------------------------------------------------
# assert_tool
# ---------------------------------------------------------------------------
def test_assert_tool_pass_on_content(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: {"result": {"content": [{"text": "ok"}]}})
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert (rep.passed, rep.failed) == (1, 0)


def test_assert_tool_fail_on_protocol_error(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: {"error": {"message": "boom"}})
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert rep.failed == 1


def test_assert_tool_fail_on_empty_content(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: {"result": {"content": []}})
    rep = Reporter("t", color=False)
    R.assert_tool(rep, "s", ADMIN, "tool", {}, "label")
    assert rep.failed == 1


# ---------------------------------------------------------------------------
# assert_tool_error
# ---------------------------------------------------------------------------
def test_assert_tool_error_pass_with_expected_substring(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: {"error": {"message": "Unknown toolset foo"}})
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "Unknown", "label")
    assert (rep.passed, rep.failed) == (1, 0)


def test_assert_tool_error_pass_on_success_false_payload(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: call_resp({"success": False, "message": "bad"}))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "", "label")
    assert rep.passed == 1


def test_assert_tool_error_fail_when_no_error(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: call_resp({"success": True}))
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "", "label")
    assert rep.failed == 1


def test_assert_tool_error_fail_on_wrong_substring(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: {"error": {"message": "some other error"}})
    rep = Reporter("t", color=False)
    R.assert_tool_error(rep, "s", ADMIN, "tool", {}, "Unknown", "label")
    assert rep.failed == 1


# ---------------------------------------------------------------------------
# _first_field
# ---------------------------------------------------------------------------
def test_first_field_returns_value(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: call_resp({"data": [{"id": "x1"}]}))
    assert R._first_field("s", ADMIN, "product") == "x1"


def test_first_field_empty_when_no_items(monkeypatch):
    monkeypatch.setattr(R, "mcp_call", lambda *a, **k: call_resp({"data": []}))
    assert R._first_field("s", ADMIN, "product") == ""


# ---------------------------------------------------------------------------
# Stateful fake server for the full flows
# ---------------------------------------------------------------------------
ADMIN_TOOLSETS = [
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

STORE_TOOLSETS = [
    {
        "name": "buyer-journey",
        "title": "Buyer Journey",
        "description": "cart/checkout",
        "enabled": False,
        "tools": [f"shopware-ucp-cart-{x}" for x in ("create", "add", "remove", "get", "update")]
        + [f"shopware-ucp-checkout-{x}" for x in ("start", "confirm")]
        + [f"shopware-ucp-catalog-{x}" for x in ("search", "read")],
    },
    {
        "name": "context",
        "title": "Context",
        "description": "context",
        "enabled": False,
        "tools": [
            "shopware-store-api-context",
            "shopware-store-config-read",
            "shopware-store-nav-read",
            "shopware-store-page-read",
            "shopware-store-seo-read",
            "shopware-store-currency-list",
        ],
    },
]


class FakeServer:
    def __init__(self, toolsets):
        self.toolsets = toolsets
        self.names = {t["name"] for t in toolsets}
        self.n = 0
        self.enabled = {}
        self.cart_items: list[dict] = []

    def init(self, endpoint=None):
        self.n += 1
        sid = f"s{self.n}"
        self.enabled[sid] = set()
        return sid, ""

    def list_toolsets(self, session, endpoint=None):
        return self.toolsets

    def enable(self, session, toolset, endpoint=None):
        self.enabled.setdefault(session, set()).add(toolset)
        return call_resp({"success": True, "_meta": {"listChanged": True}})

    def tools_list(self, session, endpoint=None):
        names = set(R.META_TOOLS)
        for ts in self.toolsets:
            if ts["name"] in self.enabled.get(session, set()):
                names.update(ts["tools"])
        return [{"name": n, "inputSchema": {"type": "object", "properties": {}}} for n in sorted(names)]

    def call(self, session, tool, args, endpoint=None):
        if tool == "shopware-entity-search":
            return call_resp({"data": [{"id": "id-1", "email": "a@b.c"}]})
        if tool == "shopware-toolset-enable":
            if args.get("toolset") not in self.names:
                return call_resp({"success": False, "message": "Unknown toolset"})
            return call_resp({"success": True, "_meta": {"listChanged": True}})
        if tool == "shopware-tool-search":
            query = args.get("query", "")
            count = min(args.get("maxResults", 5), 20)
            # Search results carry the full tool definition, inputSchema included —
            # that is what makes a surfaced tool directly callable. Omitting it here
            # would let the schema-conformance check pass against a fake that is
            # laxer than any real server.
            if "cart" in query or "shopping" in query:
                data = [{"tool": search_tool("shopware-ucp-cart-add"), "score": 0.9, "matchedIn": "desc"}]
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


def _wire(monkeypatch, fake):
    monkeypatch.setattr(R, "mcp_init", fake.init)
    monkeypatch.setattr(R, "mcp_toolsets_list", fake.list_toolsets)
    monkeypatch.setattr(R, "enable_toolset", fake.enable)
    monkeypatch.setattr(R, "enable_all_toolsets", lambda s, endpoint=None: None)
    monkeypatch.setattr(R, "mcp_tools_list_all", fake.tools_list)
    monkeypatch.setattr(R, "mcp_call", fake.call)
    # lane.py holds its own reference: the cart seeding the runner delegates to
    # lives there so the eval suite shares one definition of "a cart that can
    # be checked out". Unpatched, these two tests reached the network.
    monkeypatch.setattr(lane, "mcp_call", fake.call)


def test_run_store_flow_all_pass(monkeypatch):
    fake = FakeServer(STORE_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    assert rep.failed == 0
    assert rep.passed >= 10


def test_run_store_flow_with_granular_ucp_toolsets(monkeypatch):
    """Trunk splits UCP across several granular toolsets (cart, checkout, catalog).
    The enable-probe must come from the selected toolset — a hardcoded probe tool
    fails whenever the picked toolset does not happen to contain it."""
    granular = [
        {
            "name": "shopware-ucp-cart",
            "title": "Cart",
            "description": "cart",
            "enabled": False,
            "tools": [
                "shopware-ucp-cart-create",
                "shopware-ucp-cart-add",
                "shopware-ucp-cart-get",
                "shopware-ucp-cart-remove",
                "shopware-ucp-cart-update",
            ],
        },
        {
            "name": "shopware-ucp-checkout",
            "title": "Checkout",
            "description": "checkout",
            "enabled": False,
            "tools": ["shopware-ucp-checkout-start", "shopware-ucp-checkout-confirm"],
        },
        {
            "name": "shopware-ucp-catalog",
            "title": "Catalog",
            "description": "catalog",
            "enabled": False,
            "tools": ["shopware-ucp-catalog-search", "shopware-ucp-catalog-read"],
        },
        {
            "name": "context",
            "title": "Context",
            "description": "context",
            "enabled": False,
            "tools": ["shopware-store-api-context", "shopware-store-config-read"],
        },
        {
            "name": "misc",
            "title": "Misc",
            "description": "misc",
            "enabled": False,
            "tools": ["shopware-store-nav-read", "shopware-store-seo-read", "shopware-store-currency-list"],
        },
    ]
    fake = FakeServer(granular)
    _wire(monkeypatch, fake)
    rep = Reporter("store", color=False)
    session, _ = fake.init()
    R.run_store(rep, STORE, session)
    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]


def test_run_admin_flow_all_pass(monkeypatch):
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    args = SimpleNamespace(endpoint="admin", skip_media_upload=False, skip_dev_tools=False)
    session, _ = fake.init()
    R.run_admin(rep, ADMIN, args, session)
    assert rep.failed == 0
    assert rep.passed >= 25
    # Named explicitly, because "nothing failed" also holds when the check is
    # SKIPped for want of a cart — which is how it read in CI while the suite
    # looked green here. It must have reached checkout.
    checkout = next(r for r in rep.records if r["tool"] == "merchant-cart-checkout")
    assert checkout["status"] != "skipped", checkout


def test_schema_check_catches_empty_properties_in_the_tool_search_payload(monkeypatch):
    """The path that shipped broken three times.

    tools/list is clean here; only the tool-search payload carries
    `"properties": []`. Server-side normalization walks result.tools and never
    reaches this, so a check that only inspects tools/list passes while an
    OpenAI-compatible client rejects the whole request.
    """
    fake = FakeServer(ADMIN_TOOLSETS)
    inner = fake.call

    def call_with_malformed_search(session, tool, args, endpoint=None):
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
    assert "swag-dev-tools-list-skills" in failures[0]["error"]


def test_schema_check_passes_when_every_path_is_conformant(monkeypatch):
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    session, _ = fake.init()

    R.verify_tool_schemas(rep, session, ADMIN)

    assert rep.failed == 0, [r for r in rep.records if r["status"] == "fail"]


def test_run_admin_skips_when_no_seed_data(monkeypatch):
    """No products/orders/customers -> the data-dependent asserts skip, not fail."""
    fake = FakeServer(ADMIN_TOOLSETS)
    monkeypatch.setattr(R, "mcp_init", fake.init)

    def call_no_data(session, tool, args, endpoint=None):
        if tool == "shopware-entity-search":
            return call_resp({"data": []})
        return call_resp({"success": True, "data": {}})

    monkeypatch.setattr(R, "mcp_call", call_no_data)
    rep = Reporter("admin", color=False)
    args = SimpleNamespace(endpoint="admin", skip_media_upload=False, skip_dev_tools=False)
    session, _ = fake.init()
    R.run_admin_tools(rep, session, ADMIN, args)
    assert rep.failed == 0
    assert rep.skipped >= 8


def test_run_admin_respects_skip_flags(monkeypatch):
    fake = FakeServer(ADMIN_TOOLSETS)
    _wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)
    args = SimpleNamespace(endpoint="admin", skip_media_upload=True, skip_dev_tools=True)
    session, _ = fake.init()
    R.run_admin_tools(rep, session, ADMIN, args)
    labels = " ".join(r["label"] for r in rep.records)
    assert "shopware-media-upload" not in labels
    assert "swag-dev-tools" not in labels
