"""The stateless 2026-07-28 revision, probed until the server serves it.

mcp/sdk 0.8 serves it, and Shopware keeps both endpoints on the handshake era
with `Builder::withoutModernEra()` until shopware/shopware#19969. So today the
probe is a recorded skip, and the day it answers, the skip turns into a failure
naming the scenarios that do not exist yet, so the change cannot go unnoticed.

Measured on trunk 7a96f6086ec, both endpoints: HTTP 400, JSON-RPC -32022
`Unsupported protocol version`, `data.requested` 2026-07-28 and `data.supported`
the four handshake revisions. `requested` is what makes the skip trustworthy:
it shows the server read the revision from this request, so a refusal for any
other reason (bad auth, a malformed probe) cannot pass as "not served yet".
"""

from eval.result_schema import as_list, as_object
from functional.reporting import Reporter
from mcp_client import PROTOCOL_REVISIONS, STATELESS_REVISION, Endpoint, mcp_discover

UNSUPPORTED_VERSION = -32022

NOT_SERVED = (
    f"the server does not serve {STATELESS_REVISION}: Builder::withoutModernEra() keeps it on the handshake "
    "era until shopware/shopware#19969"
)
NO_SCENARIOS = (
    f"the server answers server/discover on {STATELESS_REVISION}, and the stateless scenarios "
    "(docs/plans/protocol-eras.md, Phase 5) are not built yet"
)


def classify(status: int, body: object) -> tuple[str, str]:
    """`("skip", reason)` for a refusal of this revision, `("fail", why)` for
    anything else. Never a pass: an answer means the scenarios are owed."""
    reply = as_object(body)
    if "result" in reply:
        return "fail", NO_SCENARIOS
    error = as_object(reply.get("error"))
    data = as_object(error.get("data"))
    if error.get("code") != UNSUPPORTED_VERSION or data.get("requested") != STATELESS_REVISION:
        return (
            "fail",
            f"server/discover answered HTTP {status} with neither a result nor a version refusal: {str(body)[:200]}",
        )
    return "skip", NOT_SERVED


def missing_revisions(body: object) -> list[str]:
    """The revisions this suite tests that the server's refusal did not list."""
    data = as_object(as_object(as_object(body).get("error")).get("data"))
    supported = {str(r) for r in as_list(data.get("supported"))}
    return [r for r in PROTOCOL_REVISIONS if r not in supported]


def verify_stateless_era(rep: Reporter, endpoint: Endpoint) -> None:
    """Ask for the stateless revision; record why it was refused, or fail."""
    rep.section(f"Stateless era ({STATELESS_REVISION})")
    label = f"stateless {STATELESS_REVISION} ({endpoint.name})"
    status, body = mcp_discover(endpoint)
    verdict, reason = classify(status, body)
    if verdict == "fail":
        rep.check_fail(label, reason)
        return
    rep.check_skip(label, reason)
    missing = missing_revisions(body)
    if missing:
        rep.check_fail(
            f"handshake revisions offered ({endpoint.name})",
            f"the server's refusal does not list {', '.join(missing)}",
        )
    else:
        rep.check_pass(f"handshake revisions offered ({endpoint.name})")
