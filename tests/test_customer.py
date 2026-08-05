"""Provisioning the customer the journey shops as.

The properties worth testing are the three that were measured the hard way: the
context token arrives in a header rather than in the body, `storefrontUrl` has to
come from the sales channel rather than from this process's configuration, and a
missing token must raise instead of degrading into a guest run under the
customer's name.
"""

from collections.abc import Mapping
from typing import cast

import pytest
import requests

from eval.result_schema import JsonObject
from functional import customer

BASE = "http://shop.example"
KEY = "sc-key"

DOMAIN = "http://storefront.example"
CONTEXT: JsonObject = {
    "salesChannel": {"domains": [{"url": DOMAIN}], "countryId": "channel-country"},
}
SALUTATIONS: JsonObject = {
    "elements": [
        {"id": "mr-id", "salutationKey": "mr"},
        {"id": "unspecified-id", "salutationKey": "not_specified"},
    ]
}
COUNTRIES: JsonObject = {"elements": [{"id": "de-id", "iso": "DE"}]}


class FakeResponse:
    def __init__(self, status: int, body: JsonObject, headers: dict[str, str] | None = None) -> None:
        self.status_code: int = status
        self._body: JsonObject = body
        # A plain dict, and the keys are spelled as the shop sends them. `requests`
        # would match either casing; these tests do not get that for free, which
        # keeps them honest about the name the code has to look for.
        self.headers: Mapping[str, str] = headers or {}

    def json(self) -> JsonObject:
        return self._body


class FakeStore:
    """A Store API that answers the paths provisioning touches.

    `logins` is a queue, because provisioning logs in more than once — to test the
    credentials, again after registering, and again after logging out for a clean
    session — and those attempts have to be able to answer differently. The last
    entry repeats, so a test that cares about only one login passes one.
    """

    def __init__(
        self, *logins: FakeResponse, register: FakeResponse | None = None, countries: JsonObject | None = None
    ) -> None:
        self.logins: list[FakeResponse] = list(logins)
        self.register: FakeResponse = register or FakeResponse(200, {})
        self.countries: JsonObject = COUNTRIES if countries is None else countries
        self.calls: list[tuple[str, JsonObject]] = []
        self.sent_tokens: list[tuple[str, str]] = []

    def request(self, _method: str, url: str, **kwargs: object) -> FakeResponse:
        path = url.removeprefix(BASE)
        self.calls.append((path, cast(JsonObject, kwargs.get("json")) or {}))
        headers = cast(dict[str, str], kwargs.get("headers") or {})
        self.sent_tokens.append((path, headers.get("sw-context-token", "")))
        if path == "/store-api/account/login":
            return self.logins.pop(0) if len(self.logins) > 1 else self.logins[0]
        if path == "/store-api/account/logout":
            return FakeResponse(200, {})
        if path == "/store-api/account/register":
            return self.register
        if path == "/store-api/context":
            return FakeResponse(200, CONTEXT)
        if path == "/store-api/salutation":
            return FakeResponse(200, SALUTATIONS)
        if path == "/store-api/country":
            return FakeResponse(200, self.countries)
        raise AssertionError(f"unexpected call to {path}")


def _install(monkeypatch: pytest.MonkeyPatch, store: FakeStore) -> FakeStore:
    monkeypatch.setattr(customer.requests, "request", store.request)
    return store


def _registration(store: FakeStore) -> JsonObject:
    return next(body for path, body in store.calls if path == "/store-api/account/register")


def test_the_context_token_is_read_from_the_response_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login body is `{"apiAlias": "array_struct", "redirectUrl": null}` — it
    carries no token at all. Reading only the body yields "", the endpoint then
    mints an anonymous token, and the journey runs as a guest while reporting
    itself as a customer."""
    store = _install(
        monkeypatch,
        FakeStore(FakeResponse(200, {"apiAlias": "array_struct"}, {"sw-context-token": "tok-1"})),
    )

    email, token = customer.provision(BASE, KEY)

    assert (email, token) == (customer.DEFAULT_EMAIL, "tok-1")
    assert not any(path == "/store-api/account/register" for path, _ in store.calls), (
        "an existing customer was re-registered"
    )


def test_the_session_is_started_clean_because_a_checkout_id_burns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shopware returns the same context token on every login for one customer, the
    journey uses that token as the checkout id, and a completed checkout id can
    never be updated again. So without logging out first, the customer half passes
    once per account and fails on every later run."""
    store = _install(
        monkeypatch,
        FakeStore(
            FakeResponse(200, {}, {"sw-context-token": "spent-token"}),
            FakeResponse(200, {}, {"sw-context-token": "fresh-token"}),
        ),
    )

    _email, token = customer.provision(BASE, KEY)

    assert token == "fresh-token"
    assert ("/store-api/account/logout", "spent-token") in store.sent_tokens, (
        "the logout has to carry the token it is ending"
    )


def test_a_token_in_the_body_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeStore(FakeResponse(200, {"contextToken": "body-token"})))

    _email, token = customer.provision(BASE, KEY)

    assert token == "body-token"


def test_an_unknown_customer_is_registered_against_the_channels_own_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`storefrontUrl` is validated against the sales channel's domains and the
    endpoint substitutes nothing, so sending the URL this process was configured
    with fails on every proxied lane."""
    store = _install(
        monkeypatch,
        FakeStore(
            FakeResponse(401, {"errors": [{"code": "CHECKOUT__CUSTOMER_AUTH_BAD_CREDENTIALS"}]}),
            FakeResponse(200, {}, {"sw-context-token": "tok-2"}),
        ),
    )

    _email, token = customer.provision(BASE, KEY)

    assert token == "tok-2"
    registration = _registration(store)
    assert registration["storefrontUrl"] == DOMAIN, "the domain has to come from the channel"
    assert registration["salutationId"] == "unspecified-id", "the buyer has no gender to state"
    assert cast(JsonObject, registration["billingAddress"])["countryId"] == "de-id"


def test_a_channel_that_does_not_sell_to_the_journeys_country_falls_back_to_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel restricted to other countries would reject the id outright, and
    its default is the one country its checkout is certain to accept."""
    store = _install(
        monkeypatch,
        FakeStore(
            FakeResponse(401, {}),
            FakeResponse(200, {}, {"sw-context-token": "tok-3"}),
            countries={"elements": []},
        ),
    )

    customer.provision(BASE, KEY)

    assert cast(JsonObject, _registration(store)["billingAddress"])["countryId"] == "channel-country"


def test_an_existing_account_with_the_wrong_password_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure that is a configuration answer rather than a bug. Reporting
    the raw `already in use` violation sends the reader looking at registration,
    which is not where the fix is."""
    _install(
        monkeypatch,
        FakeStore(
            FakeResponse(401, {}),
            register=FakeResponse(
                400,
                {
                    "errors": [
                        {
                            "code": "VIOLATION::CUSTOMER_EMAIL_NOT_UNIQUE",
                            "detail": 'The email address "x" is already in use.',
                            "source": {"pointer": "/email"},
                        }
                    ]
                },
            ),
        ),
    )

    with pytest.raises(customer.CustomerUnavailable) as excinfo:
        customer.provision(BASE, KEY)

    assert customer.PASSWORD_ENV in str(excinfo.value)
    assert customer.EMAIL_ENV in str(excinfo.value)


def test_double_opt_in_is_named_rather_than_reported_as_bad_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration succeeds, the account is inactive, and the login that follows
    fails on credentials that are correct. Without this the reported cause is the
    password, and the password is fine."""
    _install(
        monkeypatch,
        FakeStore(
            FakeResponse(401, {}),
            register=FakeResponse(200, {"doubleOptInRegistration": True, "active": False}),
        ),
    )

    with pytest.raises(customer.CustomerUnavailable, match="double opt-in"):
        customer.provision(BASE, KEY)


def test_a_login_that_returns_no_token_raises_rather_than_returning_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty token is the dangerous outcome: the caller would build an endpoint
    with a fresh anonymous one and report a customer journey it never ran."""
    _install(monkeypatch, FakeStore(FakeResponse(200, {}), register=FakeResponse(200, {})))

    with pytest.raises(customer.CustomerUnavailable, match="no context token"):
        customer.provision(BASE, KEY)


def test_the_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(customer.EMAIL_ENV, "someone@example.invalid")
    monkeypatch.setenv(customer.PASSWORD_ENV, "hunter2-hunter2")
    store = _install(monkeypatch, FakeStore(FakeResponse(200, {"contextToken": "t"})))

    email, _token = customer.provision(BASE, KEY)

    assert email == "someone@example.invalid"
    login = next(body for path, body in store.calls if path == "/store-api/account/login")
    assert login == {"email": "someone@example.invalid", "password": "hunter2-hunter2"}


def test_an_unreachable_shop_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(customer.requests, "request", boom)

    with pytest.raises(customer.CustomerUnavailable, match="could not be reached"):
        customer.provision(BASE, KEY)
