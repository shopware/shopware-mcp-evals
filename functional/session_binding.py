"""A handshake session on `/api/_mcp` belongs to the principal that opened it.

shopware/shopware#21860 binds the session to the user or integration that sent
`initialize`. Without it, anyone who learns an `Mcp-Session-Id` can use the
toolsets enabled in it, and can end it with `DELETE`. Checked here:

  foreign call    another principal's request on the session gets the same 404
                  as an unknown session, so the answer does not reveal it exists
  foreign close   another principal's `DELETE` is refused the same way, and the
                  owner's session still answers afterwards
  new token       a fresh access token of the same integration keeps the
                  session: the binding is to the principal, not the token

The other principal is an integration with "All capabilities", so the allowlist
cannot be what refuses it. Store API sessions and the stateless era are not
bound and not checked here.
"""

import requests

from functional.principals import AdminApi, Principal, ProvisioningFailed, another_principal
from functional.reporting import Reporter
from functional.sessions import probe, refused
from mcp_client import Endpoint, admin_endpoint, mcp_init

FOREIGN_CALL = "session binding: another principal's request is refused like an unknown session (admin)"
FOREIGN_CLOSE = "session binding: another principal's DELETE is refused (admin)"
OWNER_KEEPS = "session binding: the owner's session survives the foreign DELETE (admin)"
NEW_TOKEN = "session binding: a new access token of the same integration keeps the session (admin)"
LABELS = (FOREIGN_CALL, FOREIGN_CLOSE, OWNER_KEEPS, NEW_TOKEN)

NEEDS_PROVISIONING = "needs --provision-principals: it creates the integration that tries someone else's session"

# A server without the binding accepts the other principal. Recorded as a skip
# naming the PR. Delete the tolerance once `shopware.sha` includes
# shopware/shopware#21860: after that, the same symptom is a regression.
PREDATES_BINDING = (
    "another principal's request on the session was accepted: shopware/shopware#21860 is not on this build"
)


def bearer_endpoint(endpoint: Endpoint, token: str) -> Endpoint:
    """The admin endpoint, authenticated with an OAuth access token instead of the key headers."""
    return Endpoint(
        endpoint.name,
        endpoint.path,
        {"Authorization": f"Bearer {token}"},
        endpoint.base_url,
        endpoint.toolsets,
        endpoint.protocol_version,
    )


def close(session_id: str, endpoint: Endpoint) -> int:
    headers = {**endpoint.auth_headers, **endpoint.session_headers(session_id)}
    return requests.delete(endpoint.url, headers=headers, timeout=30).status_code


def refusal_problem(status: int, body: str) -> str | None:
    if refused(status, body):
        return None
    return f"HTTP {status}, not the unknown-session 404: {' '.join(body.split())[:200]}"


def verify_session_binding(rep: Reporter, endpoint: Endpoint, provision: bool) -> None:
    rep.section("Session binding (admin)")
    if not provision:
        for label in LABELS:
            rep.check_skip(label, NEEDS_PROVISIONING)
        return
    try:
        api = AdminApi(
            endpoint.base_url, endpoint.auth_headers["sw-access-key"], endpoint.auth_headers["sw-secret-access-key"]
        )
        with another_principal(api) as other:
            check_binding(rep, endpoint, other)
    except ProvisioningFailed as exc:
        rep.check_fail(FOREIGN_CALL, f"could not provision the other integration: {exc}")


def check_binding(rep: Reporter, endpoint: Endpoint, other: Principal) -> None:
    owner_session, _ = mcp_init(endpoint=endpoint)
    intruder = admin_endpoint(other.access_key, other.secret_key, base_url=endpoint.base_url).with_protocol(
        endpoint.protocol_version
    )
    status, body = probe(owner_session, intruder)
    if status == 200:
        rep.check_skip(FOREIGN_CALL, PREDATES_BINDING)
        return
    problem = refusal_problem(status, body)
    if problem:
        rep.check_fail(FOREIGN_CALL, problem)
    else:
        rep.check_pass(FOREIGN_CALL)

    close_status = close(owner_session, intruder)
    if close_status == 404:
        rep.check_pass(FOREIGN_CLOSE)
    else:
        rep.check_fail(FOREIGN_CLOSE, f"HTTP {close_status}, expected the unknown-session 404")
    owner_status, owner_body = probe(owner_session, endpoint)
    if owner_status == 200:
        rep.check_pass(OWNER_KEEPS)
    else:
        rep.check_fail(OWNER_KEEPS, f"HTTP {owner_status}: {' '.join(owner_body.split())[:200]}")

    first = AdminApi(endpoint.base_url, other.access_key, other.secret_key).token
    session, _ = mcp_init(endpoint=bearer_endpoint(endpoint, first))
    second = AdminApi(endpoint.base_url, other.access_key, other.secret_key).token
    token_status, token_body = probe(session, bearer_endpoint(endpoint, second))
    if first == second:
        rep.check_fail(NEW_TOKEN, "the token endpoint handed out the same token twice")
    elif token_status == 200:
        rep.check_pass(NEW_TOKEN)
    else:
        rep.check_fail(NEW_TOKEN, f"HTTP {token_status}: {' '.join(token_body.split())[:200]}")
