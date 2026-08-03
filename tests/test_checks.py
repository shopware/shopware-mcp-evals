"""The admin check table.

This was 261 lines of straight-line calls inside run_admin_tools, so the only
way to exercise a payload or a skip reason was to run the whole suite against a
live shop. The table is data now, and these assert the parts that used to be
silently wrong: a payload shape, and which reason a blocked check reports.
"""

import json
import pathlib
from typing import cast

import pytest

from eval.result_schema import JsonObject, McpResponse, as_list
from functional import checks as K
from functional import runner as R
from functional.checks import Context, ToolCheck
from mcp_client import ADMIN, Endpoint
from tests.stubs import const


def by_name(name: str) -> ToolCheck:
    return next(c for c in K.ALL_CHECKS if c.tool == name)


FULL_CTX: Context = {
    "product_id": "p1",
    "order_id": "o1",
    "customer_email": "a@b.c",
    "customer_id": "c1",
    "sales_channel_id": "sc1",
    "cart_token": "tok",
    # Distinct from product_id: entity-search returns products that are inactive,
    # out of stock or not in the channel, and adding one of those to a cart is a
    # silent no-op. These come from merchant-storefront-search, and there are
    # several because that search does not filter for sellability either — the
    # runner adds them until the cart reads back non-empty.
    "cart_product_ids": ["p-sellable", "p-other"],
    # `file` has no usable default — the tool answers "Log file not found" for the
    # empty string, and names the valid values in that error. `dev.log` rather
    # than a dated name on purpose: nothing here parses it, so a date would only
    # look like it mattered and read as stale the day after it was written.
    "log_file": "dev.log",
    "skill_name": "nightly-triage",
    "media_upload_enabled": True,
}


def test_every_check_has_a_distinct_label() -> None:
    """Two checks sharing a label make a failure report ambiguous."""
    labels = [c.label(FULL_CTX) for c in K.ALL_CHECKS]

    assert len(labels) == len(set(labels))


def test_every_check_builds_a_payload_from_a_full_context() -> None:
    """A typo'd context key would otherwise surface as a KeyError mid-run,
    after the suite had already spent minutes talking to the server."""
    for check in K.ALL_CHECKS:
        assert isinstance(check.args(FULL_CTX), dict), check.tool


def test_labels_carry_the_tool_name_so_output_stays_greppable() -> None:
    for check in K.ALL_CHECKS:
        assert check.label(FULL_CTX).startswith(check.tool)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
def test_a_check_with_no_prerequisites_always_runs() -> None:
    assert by_name("shopware-entity-schema").blocked_by({}) is None


def test_a_missing_prerequisite_blocks_with_its_reason() -> None:
    assert by_name("shopware-entity-read").blocked_by({}) == "no product found"


def test_an_empty_string_counts_as_missing() -> None:
    """_first_field returns '' rather than None when the shop has no such entity."""
    assert by_name("shopware-entity-read").blocked_by({"product_id": ""}) == "no product found"


def test_the_first_missing_prerequisite_decides_the_reason() -> None:
    """merchant-cart-checkout needs a sales channel, a cart in it, and a
    customer. Those are three different findings, and reporting the wrong one
    sends you looking in the wrong place — an empty `cart_token` in particular
    means no product could be added, not that checkout misbehaved."""
    checkout = by_name("merchant-cart-checkout")

    assert checkout.blocked_by({}) == "no storefront sales channel"
    assert checkout.blocked_by({"sales_channel_id": "sc1"}) == (
        "no sellable product in this channel, so no cart to check out"
    )
    assert checkout.blocked_by({"sales_channel_id": "sc1", "cart_token": "t"}) == "no customer found"
    assert checkout.blocked_by(FULL_CTX) is None


def test_media_upload_is_gated_like_any_other_prerequisite() -> None:
    """It is the one check that writes a real file, so --skip-media-upload must
    reach it — and report why it was skipped, not just that it was."""
    upload = by_name("shopware-media-upload")

    assert upload.blocked_by(FULL_CTX | {"media_upload_enabled": False}) == "--skip-media-upload"
    assert upload.blocked_by(FULL_CTX) is None


def test_skip_labels_name_the_tool_and_the_reason() -> None:
    check = by_name("shopware-order-state")

    assert check.skip_label("no order found") == "shopware-order-state (no order found)"


# ---------------------------------------------------------------------------
# Payload shapes that the server actually validates
# ---------------------------------------------------------------------------
def test_mutating_checks_all_pass_dry_run() -> None:
    """A check that mutated the shop for real would make the suite unsafe to run
    against anything but a throwaway instance."""
    for name in (
        "shopware-entity-upsert",
        "shopware-entity-delete",
        "shopware-system-config-write",
        "shopware-order-state",
        "merchant-cart-checkout",
        "merchant-product-create",
    ):
        assert by_name(name).args(FULL_CTX).get("dryRun") is True, name


def test_delete_targets_a_uuid_that_cannot_exist() -> None:
    ids = as_list(cast(object, json.loads(str(by_name("shopware-entity-delete").args(FULL_CTX)["ids"]))))

    assert ids == [K.ZERO_UUID]
    assert set(K.ZERO_UUID) <= set("0-")


@pytest.mark.parametrize(
    "name,key",
    [
        ("shopware-entity-aggregate", "aggregations"),
        ("shopware-entity-upsert", "payload"),
        ("shopware-entity-delete", "ids"),
        ("shopware-system-config-write", "value"),
    ],
)
def test_json_string_arguments_are_serialised_not_passed_as_objects(name: str, key: str) -> None:
    """These parameters are declared as strings in the tool schema; passing a
    dict or list makes the server reject the call."""
    value = by_name(name).args(FULL_CTX)[key]

    assert isinstance(value, str)
    _decoded = cast(object, json.loads(value))


def test_load_skill_names_the_skill_it_loaded() -> None:
    """`swag-dev-tools-load-skill` on its own does not say which skill passed."""
    check = by_name("swag-dev-tools-load-skill")

    assert check.label(FULL_CTX) == "swag-dev-tools-load-skill (nightly-triage)"


def test_the_table_covers_the_sections_the_runner_walks() -> None:
    assert K.ALL_CHECKS == K.CORE_CHECKS + K.MERCHANT_CHECKS + K.DEV_CHECKS
    assert all(c.tool.startswith(("shopware-", "merchant-")) for c in K.CORE_CHECKS + K.MERCHANT_CHECKS)
    assert all(c.tool.startswith("swag-dev-tools-") for c in K.DEV_CHECKS)


def test_every_context_key_a_check_uses_is_in_the_full_context() -> None:
    """FULL_CTX is the canonical "everything the server could provide". A check
    reading a key that is not in it means the runner has to supply that key and
    nothing checks that it does — which is how `log_file` shipped as a KeyError
    waiting to happen."""
    for check in K.ALL_CHECKS:
        try:
            check.args(FULL_CTX)
        except KeyError as exc:
            raise AssertionError(f"{check.tool} reads {exc} which FULL_CTX does not define") from exc


def test_the_media_filename_is_unique_per_call() -> None:
    """A fixed name uploads once and then fails with "already exists" on every
    later run against the same instance — green in CI, broken on a trunk lane."""
    check = next(c for c in K.ALL_CHECKS if c.tool == "shopware-media-upload")
    names = {str(check.args(FULL_CTX)["fileName"]) for _ in range(5)}

    assert len(names) == 5
    assert all(n.endswith(".png") for n in names), "the extension comes from fileName, not the URL"


def test_the_phantom_uuid_is_the_form_shopware_accepts() -> None:
    """The dashed form is rejected by the DAL outright, so the delete check would
    assert the argument validator rather than the tool it names."""
    assert K.ZERO_UUID == "0" * 32
    assert "-" not in K.ZERO_UUID


def test_order_state_always_sends_an_action() -> None:
    """Without one the call is rejected before reaching the state machine."""
    check = next(c for c in K.ALL_CHECKS if c.tool == "shopware-order-state")
    args = check.args(FULL_CTX)

    assert any(k in args for k in ("orderAction", "transactionAction", "deliveryAction"))


# ---------------------------------------------------------------------------
# Picking a log file to read
# ---------------------------------------------------------------------------
def _log_reply(files: list[str]) -> str:
    inner = "Log file not found. Available files: " + ", ".join(files)
    return json.dumps({"success": False, "error": inner})


def _pick(monkeypatch: pytest.MonkeyPatch, files: list[str]) -> str:
    monkeypatch.setattr(R, "mcp_call", const(cast(McpResponse, cast(object, {}))))
    monkeypatch.setattr(R, "mcp_result_text", const(_log_reply(files)))
    return R.newest_log_file("sid", ADMIN)


def test_the_newest_log_file_wins_regardless_of_the_order_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Taking the last element made a correct-looking result depend on an
    undocumented ordering in somebody else's response."""
    files = ["dev.log", "prod-2026-06-01.log", "prod-2026-07-31.log"]

    assert _pick(monkeypatch, files) == "prod-2026-07-31.log"
    assert _pick(monkeypatch, list(reversed(files))) == "prod-2026-07-31.log"


def test_an_instance_with_only_dev_log_still_gets_a_readable_file(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _pick(monkeypatch, ["dev.log"]) == "dev.log"


def test_no_log_files_yields_no_file_so_the_checks_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipped-with-a-reason, not failed: an instance that has not logged
    anything yet says nothing about whether the tool works."""
    monkeypatch.setattr(R, "mcp_call", const(cast(McpResponse, cast(object, {}))))
    monkeypatch.setattr(R, "mcp_result_text", const(json.dumps({"success": True, "data": []})))

    assert R.newest_log_file("sid", ADMIN) == ""


# ---------------------------------------------------------------------------
# The log probe: a known line, so "the reader works" is distinguishable from
# "the file happened to have something in it"
# ---------------------------------------------------------------------------
def test_the_lane_seeds_exactly_the_text_the_check_looks_for() -> None:
    """Two files have to agree on one string. If the composite action drifts from
    LOG_PROBE_TEXT the check silently falls back to its weak form and nobody
    notices, which is the failure this whole probe exists to prevent."""
    action = pathlib.Path(".github/actions/setup-lane/action.yml").read_text()

    assert K.LOG_PROBE_TEXT in action, "the lane no longer writes the line the check asserts on"


def test_the_probe_text_cannot_collide_with_shopware_output() -> None:
    """A hit has to prove the reader opened our file rather than matching
    something the framework happens to log."""
    assert "mcp-evals" in K.LOG_PROBE_TEXT
    assert any(ch.isdigit() for ch in K.LOG_PROBE_TEXT), "needs a distinctive token, not just words"


def test_log_search_asserts_on_content_not_just_a_reply() -> None:
    check = by_name("swag-dev-tools-log-search")

    assert check.contains == K.LOG_PROBE_TEXT


def test_log_search_looks_for_the_probe_when_the_lane_seeded_one() -> None:
    check = by_name("swag-dev-tools-log-search")

    seeded = check.args(FULL_CTX | {"log_probe": True})
    assert seeded["query"] == K.LOG_PROBE_TEXT
    assert "seeded line" in check.label(FULL_CTX | {"log_probe": True})


def test_log_search_falls_back_where_no_lane_seeded_one() -> None:
    """Someone else's shop has no probe, and demanding one would fail every
    instance this suite did not build."""
    check = by_name("swag-dev-tools-log-search")

    fallback = check.args(FULL_CTX | {"log_probe": False})
    assert fallback["query"] == "error"
    assert "seeded line" not in check.label(FULL_CTX | {"log_probe": False})


# ---------------------------------------------------------------------------
# Seeding a cart the checkout check can actually check out.
#
# `merchant-cart-manage action=add` answers `success: true` for a product the
# channel cannot sell and puts nothing in the cart, so every call in the chain
# was green and checkout still failed with "Cart is empty. Add items with
# merchant-cart-manage first". Reading the cart back is the only assertion that
# distinguishes the two.
# ---------------------------------------------------------------------------
class _CartServer:
    """A shop where exactly one product is sellable."""

    def __init__(self, sellable: tuple[str, ...] = ("p2",), token: str = "tok-1") -> None:
        self.sellable: set[str] = set(sellable)
        self.token: str = token
        self.items: list[JsonObject] = []
        self.added: list[str] = []

    def call(self, _sid: str, name: str, args: JsonObject, endpoint: Endpoint | None = None) -> JsonObject:
        assert endpoint is None or endpoint is ADMIN
        if name == "merchant-storefront-search":
            return {"products": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}
        action = args.get("action")
        if action == "create":
            return {"token": self.token}
        if action == "add":
            product_id = str(args["productId"])
            self.added.append(product_id)
            if product_id in self.sellable:
                self.items.append({"id": "li-1", "referencedId": product_id})
            return {}
        if action == "get":
            return {"lineItems": list(self.items)}
        return {}


@pytest.fixture
def cart_server(monkeypatch: pytest.MonkeyPatch) -> _CartServer:
    """Patches `lane`, not `functional.runner`: the seeding lives there so the
    eval suite shares one definition of a cart that can be checked out."""
    import lane

    server = _CartServer()

    def call(session: str, name: str, args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is None or endpoint is ADMIN
        return cast(McpResponse, cast(object, {"_p": server.call(session, name, args)}))

    def result_text(resp: McpResponse) -> str:
        payload = cast(JsonObject, cast(object, resp))["_p"]
        return json.dumps({"success": True, "data": payload})

    monkeypatch.setattr(lane, "mcp_call", call)
    monkeypatch.setattr(lane, "mcp_result_text", result_text)
    return server


def test_the_seeded_cart_holds_a_product_the_channel_can_actually_sell(
    cart_server: _CartServer,
) -> None:
    server = cart_server

    token = R.create_cart_token("sid", ADMIN, "sc1", R.sellable_products("sid", ADMIN, "sc1"))

    assert token == "tok-1"
    assert server.added == ["p1", "p2"], "it stopped as soon as the cart read back non-empty"


def test_a_channel_with_nothing_sellable_yields_no_token_so_checkout_skips(
    cart_server: _CartServer,
) -> None:
    """SKIP, not FAIL. A lane with no purchasable product is missing data about
    the shop, not evidence that merchant-cart-checkout is broken — and the
    check declares `cart_token` a prerequisite so an empty one reads that way."""
    server = cart_server
    server.sellable = set()

    token = R.create_cart_token("sid", ADMIN, "sc1", R.sellable_products("sid", ADMIN, "sc1"))

    assert token == ""
    assert server.added == ["p1", "p2", "p3"], "every candidate was tried before giving up"
    assert by_name("merchant-cart-checkout").blocked_by(FULL_CTX | {"cart_token": ""}) == (
        "no sellable product in this channel, so no cart to check out"
    )


@pytest.mark.usefixtures("cart_server")
def test_a_channel_without_a_storefront_yields_no_candidates() -> None:
    assert R.sellable_products("sid", ADMIN, "") == []
    assert R.create_cart_token("sid", ADMIN, "", ["p1"]) == ""
