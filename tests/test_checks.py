"""The admin check table.

This was 261 lines of straight-line calls inside run_admin_tools, so the only
way to exercise a payload or a skip reason was to run the whole suite against a
live shop. The table is data now, and these assert the parts that used to be
silently wrong: a payload shape, and which reason a blocked check reports.
"""

import json

import pytest

from functional import checks as K


def by_name(name):
    return next(c for c in K.ALL_CHECKS if c.tool == name)


FULL_CTX = {
    "product_id": "p1",
    "order_id": "o1",
    "customer_email": "a@b.c",
    "customer_id": "c1",
    "sales_channel_id": "sc1",
    "cart_token": "tok",
    # Distinct from product_id: entity-search returns products that are inactive,
    # out of stock or not in the channel, and adding one of those to a cart is a
    # silent no-op. This one comes from merchant-storefront-search.
    "cart_product_id": "p-sellable",
    # `file` has no usable default — the tool answers "Log file not found" for the
    # empty string, and names the valid values in that error.
    "log_file": "prod-2026-07-31.log",
    "skill_name": "nightly-triage",
    "media_upload_enabled": True,
}


def test_every_check_has_a_distinct_label():
    """Two checks sharing a label make a failure report ambiguous."""
    labels = [c.label(FULL_CTX) for c in K.ALL_CHECKS]

    assert len(labels) == len(set(labels))


def test_every_check_builds_a_payload_from_a_full_context():
    """A typo'd context key would otherwise surface as a KeyError mid-run,
    after the suite had already spent minutes talking to the server."""
    for check in K.ALL_CHECKS:
        assert isinstance(check.args(FULL_CTX), dict), check.tool


def test_labels_carry_the_tool_name_so_output_stays_greppable():
    for check in K.ALL_CHECKS:
        assert check.label(FULL_CTX).startswith(check.tool)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
def test_a_check_with_no_prerequisites_always_runs():
    assert by_name("shopware-entity-schema").blocked_by({}) is None


def test_a_missing_prerequisite_blocks_with_its_reason():
    assert by_name("shopware-entity-read").blocked_by({}) == "no product found"


def test_an_empty_string_counts_as_missing():
    """_first_field returns '' rather than None when the shop has no such entity."""
    assert by_name("shopware-entity-read").blocked_by({"product_id": ""}) == "no product found"


def test_the_first_missing_prerequisite_decides_the_reason():
    """merchant-cart-checkout needs a sales channel AND a cart in it. 'no
    storefront sales channel' and 'could not get cart token' are different
    findings, and reporting the wrong one sends you looking in the wrong place."""
    checkout = by_name("merchant-cart-checkout")

    assert checkout.blocked_by({}) == "no storefront sales channel"
    assert checkout.blocked_by({"sales_channel_id": "sc1"}) == "could not get cart token or customer ID"
    assert (
        checkout.blocked_by({"sales_channel_id": "sc1", "cart_token": "t"}) == "could not get cart token or customer ID"
    )
    assert checkout.blocked_by(FULL_CTX) is None


def test_media_upload_is_gated_like_any_other_prerequisite():
    """It is the one check that writes a real file, so --skip-media-upload must
    reach it — and report why it was skipped, not just that it was."""
    upload = by_name("shopware-media-upload")

    assert upload.blocked_by(FULL_CTX | {"media_upload_enabled": False}) == "--skip-media-upload"
    assert upload.blocked_by(FULL_CTX) is None


def test_skip_labels_name_the_tool_and_the_reason():
    check = by_name("shopware-order-state")

    assert check.skip_label("no order found") == "shopware-order-state (no order found)"


# ---------------------------------------------------------------------------
# Payload shapes that the server actually validates
# ---------------------------------------------------------------------------
def test_mutating_checks_all_pass_dry_run():
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


def test_delete_targets_a_uuid_that_cannot_exist():
    ids = json.loads(by_name("shopware-entity-delete").args(FULL_CTX)["ids"])

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
def test_json_string_arguments_are_serialised_not_passed_as_objects(name, key):
    """These parameters are declared as strings in the tool schema; passing a
    dict or list makes the server reject the call."""
    value = by_name(name).args(FULL_CTX)[key]

    assert isinstance(value, str)
    json.loads(value)


def test_load_skill_names_the_skill_it_loaded():
    """`swag-dev-tools-load-skill` on its own does not say which skill passed."""
    check = by_name("swag-dev-tools-load-skill")

    assert check.label(FULL_CTX) == "swag-dev-tools-load-skill (nightly-triage)"


def test_the_table_covers_the_sections_the_runner_walks():
    assert K.ALL_CHECKS == K.CORE_CHECKS + K.MERCHANT_CHECKS + K.DEV_CHECKS
    assert all(c.tool.startswith(("shopware-", "merchant-")) for c in K.CORE_CHECKS + K.MERCHANT_CHECKS)
    assert all(c.tool.startswith("swag-dev-tools-") for c in K.DEV_CHECKS)


def test_every_context_key_a_check_uses_is_in_the_full_context():
    """FULL_CTX is the canonical "everything the server could provide". A check
    reading a key that is not in it means the runner has to supply that key and
    nothing checks that it does — which is how `log_file` shipped as a KeyError
    waiting to happen."""
    for check in K.ALL_CHECKS:
        try:
            check.args(FULL_CTX)
        except KeyError as exc:
            raise AssertionError(f"{check.tool} reads {exc} which FULL_CTX does not define") from exc


def test_the_media_filename_is_unique_per_call():
    """A fixed name uploads once and then fails with "already exists" on every
    later run against the same instance — green in CI, broken on a trunk lane."""
    check = next(c for c in K.ALL_CHECKS if c.tool == "shopware-media-upload")
    names = {check.args(FULL_CTX)["fileName"] for _ in range(5)}

    assert len(names) == 5
    assert all(n.endswith(".png") for n in names), "the extension comes from fileName, not the URL"


def test_the_phantom_uuid_is_the_form_shopware_accepts():
    """The dashed form is rejected by the DAL outright, so the delete check would
    assert the argument validator rather than the tool it names."""
    assert K.ZERO_UUID == "0" * 32
    assert "-" not in K.ZERO_UUID


def test_order_state_always_sends_an_action():
    """Without one the call is rejected before reaching the state machine."""
    check = next(c for c in K.ALL_CHECKS if c.tool == "shopware-order-state")
    args = check.args(FULL_CTX)

    assert any(k in args for k in ("orderAction", "transactionAction", "deliveryAction"))
