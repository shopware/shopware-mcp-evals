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
