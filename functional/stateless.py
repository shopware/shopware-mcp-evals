"""The stateless 2026-07-28 revision, checked against the degradation ADR.

On that revision there is no `initialize` and no session: every request carries
its revision and client capabilities in `params._meta`. Core serves it next to
the handshake revisions when `shopware.mcp.protocol_era` is `auto` for an
endpoint (shopware/shopware#19969). The default, `legacy`, refuses it.

A refusal is a recorded skip, and it only counts as one when it names the
revision this probe asked for (`data.requested`), so a refusal for any other
reason (bad auth, a malformed probe) cannot pass as "not served".

When the server answers, the scenarios below run. They assert what
`adr/2026-09-24-mcp-stateless-era-degradation-policy.md` in shopware/shopware
decides, not whatever the server happens to do:

- no `Mcp-Session-Id` is minted
- `?toolsets=` pins the same tools on both eras
- `shopware-toolset-enable` and `shopware-toolsets-list` are not offered, and
  calling them answers `isError` pointing to `?toolsets=`
- a large result comes back as a signed `resource_link` that the next request
  of the same principal reads, on either era
- a `DELETE` without a session is a handshake request and answers 400
- `subscriptions/listen` acknowledges at once, carries `tools/list_changed`
  after an allowlist edit (Admin), and closes before PHP's default
  `max_execution_time` of 30 s would kill the worker; the Store endpoint,
  which has no list changes to deliver, does not advertise them
- answers carry private SEP-2549 cache hints: 60 s for discovery and lists,
  one hour or five minutes for reference resources, none for a tool result,
  and no hint at all on the handshake revisions
"""

from collections.abc import Callable

import requests

from eval.result_schema import JsonObject, as_list, as_object
from functional.large_results import (
    NO_STORE_OFFLOAD,
    OFFLOAD_ARGS,
    OFFLOAD_TOOL,
    offload_problems,
    pointer,
    readable_problem,
)
from functional.principals import PARTIAL, AdminApi, ProvisioningFailed, another_principal
from functional.reporting import Reporter
from mcp_client import (
    ACKNOWLEDGED,
    PROTOCOL_REVISIONS,
    STATELESS_REVISION,
    Endpoint,
    ListenFrame,
    StatelessReply,
    mcp_discover,
    mcp_init,
    mcp_listen,
    mcp_read_resource,
    mcp_stateless,
    mcp_stateless_tools,
    mcp_tools_list_all,
    mcp_toolsets_list,
)
from mcp_result import read_result

UNSUPPORTED_VERSION = -32022

NOT_SERVED = (
    f"the server does not serve {STATELESS_REVISION}: `shopware.mcp.protocol_era` is `legacy` for this "
    "endpoint, or core predates shopware/shopware#19969"
)

# The meta-tools that keep state in the MCP session. The ADR lets the stateless
# era hide them or answer with an error; core does both.
SESSION_TOOLS = ("shopware-toolset-enable", "shopware-toolsets-list")
RECONNECT_HINT = "?toolsets="

# The acknowledgement MUST be the first message of a listen stream, and a
# client waits for it before it trusts the stream. Seconds, generous for CI.
ACK_WITHIN_S = 5.0
# `McpListVersionsNotificationBus` caches the versions for one second and the
# stream polls four times a second, so a change arrives within about 1.5 s.
LIST_CHANGE_WITHIN_S = 5.0
# PHP's default `max_execution_time`. A stream still open then is killed with
# its worker instead of being closed; core holds the Admin one for 25 s.
WORKER_LIMIT_S = 30.0
LISTEN_TIMEOUT_S = WORKER_LIMIT_S + 10
LIST_CHANGED = "notifications/tools/list_changed"
NO_STORE_LIST_CHANGES = (
    "the Store endpoint has no installation-wide list changes to publish: its only one was the "
    "session-local toolset enable"
)
NEEDS_PROVISIONING = "needs --provision-principals: it creates an integration whose allowlist it edits"
STORE_LIST_CHANGED_LABEL = "stateless: server/discover does not advertise tools.listChanged (store)"
# Before shopware/shopware#22032 the Store server announced list changes it
# never delivers on this era. Recorded as a skip naming the PR. Delete the
# tolerance once `shopware.sha` includes it: after that, the symptom is a regression.
PREDATES_STORE_LIST_CHANGED = (
    "the Store server/discover advertises tools.listChanged: shopware/shopware#22032 is not on this build"
)

# SEP-2549 cache hints (shopware/shopware#22033). Every hint is `private`,
# because the allowlist and `?toolsets=` make answers differ per caller.
CACHE_SCOPE = "private"
# Lists and discovery: an allowlist change reaches a client within a minute.
LIST_TTL_MS = 60_000
# Reference resources that rarely change, one per TTL core uses.
REFERENCE_TTL_MS = {"shopware://entities": 3_600_000, "shopware://currencies": 60_000}
CACHE_HINT_TOOL = "shopware-tool-search"
CACHE_HINT_ARGS: JsonObject = {"query": "product", "maxResults": 1}
# A server without the hints sends none on server/discover. Recorded as a skip
# naming the PR. Delete the tolerance once `shopware.sha` includes
# shopware/shopware#22033: after that, the same symptom is a regression.
PREDATES_CACHE_HINTS = "server/discover carries no ttlMs or cacheScope: shopware/shopware#22033 is not on this build"


def classify(status: int, body: object) -> tuple[str, str]:
    """`("serve", "")` when the server answers on this revision, `("skip",
    reason)` for a refusal of it, `("fail", why)` for anything else."""
    reply = as_object(body)
    if "result" in reply:
        versions = as_list(as_object(reply.get("result")).get("supportedVersions"))
        if STATELESS_REVISION not in versions:
            return "fail", f"server/discover answered without {STATELESS_REVISION} in supportedVersions: {versions}"
        return "serve", ""
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


class Era:
    """The stateless requests of one run, so the session-id check sees all of them."""

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint: Endpoint = endpoint
        self.minted: set[str] = set()

    def call(self, method: str, params: JsonObject | None = None) -> StatelessReply:
        reply = mcp_stateless(method, params, self.endpoint)
        if reply.session_id:
            self.minted.add(reply.session_id)
        return reply


def _report(rep: Reporter, label: str, problem: str | None) -> None:
    if problem:
        rep.check_fail(label, problem)
    else:
        rep.check_pass(label)


def verify_stateless_era(rep: Reporter, endpoint: Endpoint, provision: bool = False) -> None:
    """Ask for the stateless revision; record why it was refused, or check it."""
    rep.section(f"Stateless era ({STATELESS_REVISION})")
    label = f"stateless {STATELESS_REVISION} ({endpoint.name})"
    status, body = mcp_discover(endpoint)
    verdict, reason = classify(status, body)
    if verdict == "fail":
        rep.check_fail(label, reason)
        return
    if verdict == "skip":
        rep.check_skip(label, reason)
        missing = missing_revisions(body)
        if missing:
            rep.check_fail(
                f"handshake revisions offered ({endpoint.name})",
                f"the server's refusal does not list {', '.join(missing)}",
            )
        else:
            rep.check_pass(f"handshake revisions offered ({endpoint.name})")
        return

    rep.check_pass(f"{label}: server/discover answers")
    era = Era(endpoint)
    verify_session_tools(rep, era)
    verify_pinned_toolsets(rep, era)
    verify_delete(rep, endpoint)
    if endpoint.name == "admin":
        verify_large_result(rep, era)
    else:
        rep.check_skip(f"stateless: a large result is a signed resource_link ({endpoint.name})", NO_STORE_OFFLOAD)
    discovered = as_object(as_object(body).get("result"))
    verify_cache_hints(rep, era, discovered)
    verify_listen(rep, endpoint, as_object(discovered.get("capabilities")), provision)
    _report(
        rep,
        f"stateless: no Mcp-Session-Id is minted ({endpoint.name})",
        f"the server minted {sorted(era.minted)}" if era.minted else None,
    )


def session_tool_problem(advertised: bool, reply: StatelessReply) -> str | None:
    """None when a session meta-tool is hidden and refuses with the reconnect hint."""
    problems: list[str] = []
    if advertised:
        problems.append("advertised in tools/list")
    if reply.error:
        problems.append(f"the call failed on the transport: {reply.error.get('message')}")
    else:
        outcome = read_result(reply.message)
        if outcome.ok:
            problems.append("the call succeeded, so the client believes something changed")
        elif RECONNECT_HINT not in outcome.failure:
            problems.append(f"the error does not point to {RECONNECT_HINT}: {outcome.failure[:200]}")
    return "; ".join(problems) or None


def verify_session_tools(rep: Reporter, era: Era) -> None:
    advertised = set(mcp_stateless_tools(era.endpoint))
    for tool in SESSION_TOOLS:
        arguments: JsonObject = {"toolset": "any"} if tool == "shopware-toolset-enable" else {}
        reply = era.call("tools/call", {"name": tool, "arguments": arguments})
        _report(
            rep,
            f"stateless: {tool} is not offered and points to {RECONNECT_HINT} ({era.endpoint.name})",
            session_tool_problem(tool in advertised, reply),
        )


def pinned_problem(handshake: list[str], stateless: list[str], again: list[str]) -> str | None:
    """None when both eras pin the same tools, apart from the session meta-tools,
    and a repeated stateless request lists exactly the same."""
    problems: list[str] = []
    want = set(handshake) - set(SESSION_TOOLS)
    got = set(stateless) - set(SESSION_TOOLS)
    if want != got:
        problems.append(f"only on handshake: {sorted(want - got)}, only on stateless: {sorted(got - want)}")
    if stateless != again:
        problems.append("a second identical request listed other tools or another order")
    return "; ".join(problems) or None


def verify_pinned_toolsets(rep: Reporter, era: Era) -> None:
    label = f"stateless: ?toolsets= pins the same tools as the handshake era ({era.endpoint.name})"
    session, _ = mcp_init(endpoint=era.endpoint)
    toolsets = [str(t.get("name", "")) for t in mcp_toolsets_list(session, era.endpoint) if t.get("tools")]
    if not toolsets:
        rep.check_skip(label, "the endpoint lists no toolset with tools to pin")
        return
    pinned = era.endpoint.with_toolsets(toolsets[0])
    pinned_session, _ = mcp_init(endpoint=pinned)
    handshake = [str(t.get("name", "")) for t in mcp_tools_list_all(pinned_session, pinned)]
    first = mcp_stateless_tools(pinned)
    _report(rep, label, pinned_problem(handshake, first, mcp_stateless_tools(pinned)))


def verify_delete(rep: Reporter, endpoint: Endpoint) -> None:
    """A stateless client never sends DELETE. A server that serves both eras
    reads one without a session id as a handshake request, which those
    revisions answer with 400 (the ADR, as amended for dual-era serving)."""
    status = requests.delete(endpoint.url, headers=endpoint.auth_headers, timeout=30).status_code
    _report(
        rep,
        f"stateless: DELETE without a session answers 400 ({endpoint.name})",
        None if status == 400 else f"HTTP {status}",
    )


def large_result_problems(reply: StatelessReply) -> tuple[str | None, list[str]]:
    """The pointer of a stateless offload, and everything wrong with it, by the
    rules of every revision since 2025-06-18: a signed `resource_link` block and
    nothing inline."""
    outcome = read_result(reply.message)
    link = pointer(outcome)
    return (link.uri if link else None), offload_problems(outcome, STATELESS_REVISION)


def verify_large_result(rep: Reporter, era: Era) -> None:
    uri, problems = large_result_problems(era.call("tools/call", {"name": OFFLOAD_TOOL, "arguments": OFFLOAD_ARGS}))
    _report(rep, "stateless: a large result is a signed resource_link (admin)", "; ".join(problems) or None)
    if uri is None or problems:
        return
    read = era.call("resources/read", {"uri": uri})
    _report(
        rep,
        "stateless: the next stateless request of the principal reads it",
        readable_problem(read.message, STATELESS_REVISION),
    )
    session, _ = mcp_init(endpoint=era.endpoint)
    _report(
        rep,
        "stateless: a handshake session of the principal reads it",
        readable_problem(mcp_read_resource(session, uri, era.endpoint), STATELESS_REVISION),
    )


def hint_problem(reply: StatelessReply | JsonObject, ttl_ms: int | None) -> str | None:
    """None when an answer carries the cache hint it should.

    `ttl_ms` None is an answer that must not be cached: no TTL or 0, and a
    scope, if any, of `private`. A `StatelessReply` that failed is a problem of
    its own, so a refusal cannot pass as "no hint"."""
    if isinstance(reply, StatelessReply):
        if reply.error:
            return f"the request failed (HTTP {reply.status}): {reply.error.get('message')}"
        result = reply.result
    else:
        result = reply
    ttl, scope = result.get("ttlMs"), result.get("cacheScope")
    if ttl_ms is None:
        if ttl not in (None, 0) or scope not in (None, CACHE_SCOPE):
            return f"ttlMs={ttl}, cacheScope={scope}: the answer may be cached"
        return None
    if ttl != ttl_ms or scope != CACHE_SCOPE:
        return f"ttlMs={ttl}, cacheScope={scope}, expected ttlMs={ttl_ms}, cacheScope={CACHE_SCOPE}"
    return None


def verify_cache_hints(rep: Reporter, era: Era, discovered: JsonObject) -> None:
    name = era.endpoint.name
    if "ttlMs" not in discovered and "cacheScope" not in discovered:
        rep.check_skip(f"stateless: cache hints ({name})", PREDATES_CACHE_HINTS)
        return
    _report(
        rep,
        f"stateless: server/discover may be cached for 60 s, privately ({name})",
        hint_problem(discovered, LIST_TTL_MS),
    )
    _report(
        rep,
        f"stateless: tools/list may be cached for 60 s, privately ({name})",
        hint_problem(era.call("tools/list"), LIST_TTL_MS),
    )
    _report(
        rep,
        f"stateless: a tool result is not cached ({name})",
        hint_problem(era.call("tools/call", {"name": CACHE_HINT_TOOL, "arguments": CACHE_HINT_ARGS}), None),
    )
    if name != "admin":
        return
    for uri, ttl_ms in REFERENCE_TTL_MS.items():
        _report(
            rep,
            f"stateless: {uri} may be cached for {ttl_ms // 60_000} min, privately (admin)",
            hint_problem(era.call("resources/read", {"uri": uri}), ttl_ms),
        )
    session, _ = mcp_init(endpoint=era.endpoint)
    handshake = as_object(mcp_read_resource(session, "shopware://entities", era.endpoint).get("result"))
    hinted = [key for key in ("ttlMs", "cacheScope") if key in handshake]
    _report(
        rep,
        "handshake: resources/read carries no cache hint (admin)",
        f"carries {hinted} on {era.endpoint.protocol_version}" if hinted else None,
    )


def listen_problems(frames: list[ListenFrame], expect_change: bool) -> dict[str, str | None]:
    """What is wrong with one listen stream, per check: the acknowledgement,
    the list change after it, and how the stream ended."""
    if frames and "status" in frames[0].message:
        refused = f"subscriptions/listen was not streamed: {str(frames[0].message)[:200]}"
        return {"ack": refused, "change": refused, "close": refused}
    problems: dict[str, str | None] = {"ack": None, "change": None, "close": None}
    ack = next((f for f in frames if f.method == ACKNOWLEDGED), None)
    if ack is None:
        problems["ack"] = "no acknowledgement on the stream"
    elif frames[0] is not ack:
        problems["ack"] = f"the first message was {frames[0].method or frames[0].message}, not the acknowledgement"
    elif ack.at > ACK_WITHIN_S:
        problems["ack"] = f"acknowledged only after {ack.at:.1f} s: the stream is buffered until it ends"

    if expect_change:
        change = next((f for f in frames if f.method == LIST_CHANGED), None)
        if change is None:
            problems["change"] = "no tools/list_changed after the allowlist edit"
        elif ack is not None and change.at - ack.at > LIST_CHANGE_WITHIN_S:
            problems["change"] = f"tools/list_changed arrived {change.at - ack.at:.1f} s after the edit"

    closing = frames[-1] if frames else None
    if closing is None or closing.message.get("result") is None:
        problems["close"] = f"the server did not close the stream with a result within {LISTEN_TIMEOUT_S:.0f} s"
    elif closing.at >= WORKER_LIMIT_S:
        problems["close"] = (
            f"closed after {closing.at:.1f} s, at or past PHP's default max_execution_time of "
            f"{WORKER_LIMIT_S:.0f} s, which kills the worker instead"
        )
    return problems


def verify_listen(rep: Reporter, endpoint: Endpoint, capabilities: JsonObject, provision: bool) -> None:
    """One stream per endpoint, held until the server closes it. A stream holds
    a PHP worker for its whole lifetime, so the checks share one."""
    name = endpoint.name
    advertised = as_object(capabilities.get("tools")).get("listChanged") is True
    change_label = f"stateless: tools/list_changed arrives through subscriptions/listen ({name})"
    edited: list[bool] = []

    def stream(on_ack: Callable[[], None] | None = None) -> list[ListenFrame]:
        return mcp_listen(endpoint, {"toolsListChanged": True}, on_ack=on_ack, timeout_s=LISTEN_TIMEOUT_S)

    if name != "admin":
        rep.check_skip(change_label, NO_STORE_LIST_CHANGES)
        if advertised:
            rep.check_skip(STORE_LIST_CHANGED_LABEL, PREDATES_STORE_LIST_CHANGED)
        else:
            rep.check_pass(STORE_LIST_CHANGED_LABEL)
        frames = stream()
    elif not advertised:
        rep.check_fail(change_label, "server/discover does not advertise tools.listChanged")
        frames = stream()
    elif not provision:
        rep.check_skip(change_label, NEEDS_PROVISIONING)
        frames = stream()
    else:
        try:
            api = AdminApi(
                endpoint.base_url, endpoint.auth_headers["sw-access-key"], endpoint.auth_headers["sw-secret-access-key"]
            )
            with another_principal(api) as other:

                def edit() -> None:
                    api.call("POST", other.allowlist_path, {"allowlist": PARTIAL})
                    edited.append(True)

                frames = stream(edit)
        except ProvisioningFailed as exc:
            rep.check_fail(change_label, f"could not provision the integration to edit: {exc}")
            frames = stream()

    problems = listen_problems(frames, expect_change=bool(edited))
    _report(rep, f"stateless: subscriptions/listen acknowledges at once ({name})", problems["ack"])
    if edited:
        _report(rep, change_label, problems["change"])
    _report(rep, f"stateless: subscriptions/listen closes before the worker limit ({name})", problems["close"])
