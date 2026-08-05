#!/usr/bin/env python3
"""A real, logged-in Shopware customer for the journey to shop as.

The journey used to run one way only: as a guest, with the buyer's identity
carried in the `buyer` block. That leaves the more interesting half of the
checkout untested, and it is the half where the two ends of the flow have to
agree — an order placed for a real account is one the same caller can read back,
and a guest order is not (see the refusal the guest journey now asserts).

So this module produces a customer and a context token to shop with. Three
measured facts shaped it:

  * **No usable customer exists to borrow.** Neither `test@example.com` nor any
    other well-known demo login authenticates on a bootstrapped lane, so the
    account has to be created. Registration is a Store API call needing no admin
    credentials, which keeps this out of CI's setup step.

  * **`storefrontUrl` must be one of the sales channel's own domains.** Sending
    the URL the suite was configured with fails validation on any proxied lane
    (`http://localhost:8100` against a channel published at
    `http://trunk.localhost:8088`), and the endpoint refuses rather than
    substituting. `GET /store-api/context` carries `salesChannel.domains[].url`,
    so the domain is discovered rather than guessed.

  * **The context token comes back in the `sw-context-token` header**, not in the
    login body — the body is `{"apiAlias": "array_struct", "redirectUrl": null}`.
    Reading only the body yields an empty token, the endpoint then mints an
    anonymous one, and the journey runs as a guest while claiming to be a
    customer. That is the failure this module exists to make impossible, so
    `provision()` returns a token or raises.

The account is deliberately identifiable: an `example.invalid` address that can
only be this suite, so a leftover customer in a dev shop is recognisable at a
glance rather than looking like a real one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import cast

import requests

from eval.result_schema import JsonObject, as_list, as_object
from functional.journeys import ADDRESS

# Both overridable, so an instance that already has a customer can point the
# journey at it instead of registering another one.
EMAIL_ENV = "UCP_JOURNEY_CUSTOMER_EMAIL"
PASSWORD_ENV = "UCP_JOURNEY_CUSTOMER_PASSWORD"

DEFAULT_EMAIL = "mcp-evals-customer@example.invalid"
# Shopware requires 8+ characters. Not a secret in any meaningful sense: it
# unlocks a synthetic account on a throwaway lane, and it has to be known to
# both the run that registers and the run that logs in.
DEFAULT_PASSWORD = "mcp-evals-2026"

TIMEOUT_S = 30


class CustomerUnavailable(RuntimeError):
    """No customer could be logged in, with the reason in the message.

    Raised rather than returned as an empty token: a caller that silently
    continued would run the guest flow under the customer's name and report
    coverage it does not have.
    """


def provision(base_url: str, access_key: str) -> tuple[str, str]:
    """Log the journey's customer in, registering it first if it does not exist.

    Returns `(email, context_token)`. The token is the Shopware context token to
    send as `sw-context-token`, which is what makes the checkout belong to this
    customer — see `functional.journeys.Persona`.
    """
    email = os.environ.get(EMAIL_ENV, "") or DEFAULT_EMAIL
    password = os.environ.get(PASSWORD_ENV, "") or DEFAULT_PASSWORD
    base = base_url.rstrip("/")

    token = _login(base, access_key, email, password)
    if not token:
        _register(base, access_key, email, password)
        token = _login(base, access_key, email, password)
        if not token:
            raise CustomerUnavailable(f"registered {email} but the login that followed returned no context token")

    return email, _fresh_session(base, access_key, token, email, password)


def _fresh_session(base: str, access_key: str, token: str, email: str, password: str) -> str:
    """A context token no previous run has spent, by logging out and back in.

    Measured, and the reason this function exists: **Shopware hands the same
    context token back on every login** for the same customer — two logins in a
    row return one token, and a client-supplied `sw-context-token` on the login is
    ignored in favour of it. Logging out first deletes that context, so the next
    login mints a new one.

    That matters because the journey passes the token as `cart_id`, and against a
    plugin without agentic-commerce#162 a spent checkout id can never be used
    again: `CheckoutCompletionStore` keeps the record keyed by checkout id,
    permanently, so the second run's `checkout.update` is refused with `Completed
    checkout sessions cannot be updated.` The customer half would pass exactly once
    per account and fail on every run after that.

    Finding this is what produced #162, which separates the checkout id from the
    context token so a cart can order twice. This stays for two reasons: the suite
    has to run against plugin versions from before that fix, and a clean session
    per run is the honest starting state either way.
    """
    _call(base, access_key, "POST", "/store-api/account/logout", None, token=token)

    fresh = _login(base, access_key, email, password)
    if not fresh:
        raise CustomerUnavailable(f"logged {email} out to start a clean session and could not log it back in")

    return fresh


def _login(base: str, access_key: str, email: str, password: str) -> str:
    """The context token for a successful login, or "" for bad credentials."""
    status, body, headers = _call(
        base, access_key, "POST", "/store-api/account/login", {"email": email, "password": password}
    )
    if status != 200:
        return ""

    # Body first for older versions that carried it there, header for current
    # ones. Whichever answers, an empty result here has to stay empty rather
    # than falling back to a fresh anonymous token.
    token = str(body.get("contextToken", "")) or headers.get("sw-context-token", "")

    return token


def _register(base: str, access_key: str, email: str, password: str) -> None:
    """Create the customer, or explain why it could not be created."""
    storefront_url = _storefront_url(base, access_key)
    payload: JsonObject = {
        "email": email,
        "password": password,
        "salutationId": _salutation_id(base, access_key),
        "firstName": str(ADDRESS["first_name"]),
        "lastName": str(ADDRESS["last_name"]),
        "acceptedDataProtection": True,
        "storefrontUrl": storefront_url,
        "billingAddress": {
            "street": str(ADDRESS["street_address"]),
            "zipcode": str(ADDRESS["postal_code"]),
            "city": str(ADDRESS["address_locality"]),
            "countryId": _country_id(base, access_key),
        },
    }

    status, body, _ = _call(base, access_key, "POST", "/store-api/account/register", payload)
    if status == 200:
        # A channel with double opt-in registers the account inactive and mails a
        # confirmation link. Registration "succeeded" and the login that follows
        # then fails on credentials that are correct, which is the most misleading
        # failure this module can produce — so it names the setting instead.
        if body.get("doubleOptInRegistration") or body.get("active") is False:
            raise CustomerUnavailable(
                f"{email} was registered but needs email confirmation (double opt-in is on for this sales "
                f"channel), so it cannot be logged in here — turn it off, or point {EMAIL_ENV} at a confirmed account"
            )
        return

    violations = _violations(body)
    if "already in use" in violations:
        # The one failure that is a configuration answer rather than a bug: the
        # account is there and the password this run holds is not its password.
        # Reporting the raw violation instead sends the reader looking for a
        # registration problem, which is not where the fix is.
        raise CustomerUnavailable(
            f"{email} exists but the password did not authenticate it — set {PASSWORD_ENV} to the real one, "
            f"or point {EMAIL_ENV} at another account"
        )

    raise CustomerUnavailable(f"registering {email} failed: {violations or status}")


def _storefront_url(base: str, access_key: str) -> str:
    """A domain the register endpoint will accept.

    It validates `storefrontUrl` against the sales channel's domains and does
    not fall back, so the value has to come from the channel rather than from
    this process's own configuration — those differ on every proxied lane.
    """
    _status, context, _headers = _call(base, access_key, "GET", "/store-api/context", None)
    domains = as_list(as_object(context.get("salesChannel")).get("domains"))
    for domain in domains:
        if url := str(as_object(domain).get("url", "")):
            return url

    raise CustomerUnavailable("the sales channel publishes no domain, so registration has no valid storefrontUrl")


def _salutation_id(base: str, access_key: str) -> str:
    _status, body, _headers = _call(base, access_key, "GET", "/store-api/salutation", None)
    salutations = [as_object(row) for row in as_list(body.get("elements"))]
    if not salutations:
        raise CustomerUnavailable("the instance has no salutations, which registration requires")

    # `not_specified` when it exists: the journey's buyer has no gender to state,
    # and picking one to satisfy a required field would be inventing data.
    for salutation in salutations:
        if salutation.get("salutationKey") == "not_specified":
            return str(salutation.get("id", ""))

    return str(salutations[0].get("id", ""))


def _country_id(base: str, access_key: str) -> str:
    """The id of the country the journey's address is in.

    Falls back to the sales channel's own country: a channel that does not sell
    to Germany would reject the id outright, and its default is the one address
    the checkout is certain to accept.
    """
    iso = str(ADDRESS["address_country"])
    _status, body, _headers = _call(
        base,
        access_key,
        "POST",
        "/store-api/country",
        {"limit": 100, "filter": [{"type": "equals", "field": "iso", "value": iso}]},
    )
    for row in as_list(body.get("elements")):
        if country_id := str(as_object(row).get("id", "")):
            return country_id

    _status, context, _headers = _call(base, access_key, "GET", "/store-api/context", None)
    if country_id := str(as_object(context.get("salesChannel")).get("countryId", "")):
        return country_id

    raise CustomerUnavailable(f"no country matches {iso} and the sales channel names no default")


def _violations(body: JsonObject) -> str:
    """The pointer and detail of a constraint violation, which is where the
    answer is — `400 Constraint violation error` alone names nothing."""
    reasons: list[str] = []
    for row in as_list(body.get("errors")):
        error = as_object(row)
        pointer = str(as_object(error.get("source")).get("pointer", ""))
        reasons.append(f"{pointer} {error.get('detail', '')}".strip())

    return "; ".join(reasons)


def _call(
    base: str, access_key: str, method: str, path: str, body: JsonObject | None, token: str = ""
) -> tuple[int, JsonObject, Mapping[str, str]]:
    """One Store API call, as (status, body, headers).

    The headers are returned because the login token is only there. `requests`
    matches header names case-insensitively, so callers read the name the
    documentation uses rather than the casing a server happened to send.
    """
    headers = {"sw-access-key": access_key, "Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["sw-context-token"] = token
    try:
        response = requests.request(
            method,
            f"{base}{path}",
            headers=headers,
            json=body,  # pyright: ignore[reportArgumentType]
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise CustomerUnavailable(f"{method} {path} could not be reached: {exc}") from exc

    try:
        parsed = as_object(cast(object, response.json()))
    except ValueError:
        parsed = {}

    return response.status_code, parsed, cast(Mapping[str, str], response.headers)
