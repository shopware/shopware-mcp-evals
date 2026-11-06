"""The MCP tool annotations core declares, as a client reads them.

shopware/shopware#22058 gives every core tool the four hints of the spec
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). A client
uses them to decide whether to ask the user before a call, so a wrong hint is
a wrong confirmation prompt, not a cosmetic issue. They are checked where a
client reads them: in `tools/list` with every toolset enabled, and inside the
tool definitions `shopware-tool-search` returns for a deferred tool.

The expected values are the ones core's `McpToolAnnotationsTest` pins. Only
the core tools are checked; an extension tool may declare whatever it needs.
"""

from eval.result_schema import JsonObject, as_list, as_object
from functional.reporting import Reporter
from mcp_client import Endpoint, enable_all_toolsets, mcp_call, mcp_init, mcp_tools_list_all
from mcp_result import read_result

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

READ_ONLY: JsonObject = {"readOnlyHint": True, "openWorldHint": False}
# Enabling a toolset changes session state, never shop data, and repeating it changes nothing.
SESSION_STATE: JsonObject = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _writes(destructive: bool, idempotent: bool, open_world: bool = False) -> JsonObject:
    return {
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


ADMIN_EXPECTED: dict[str, JsonObject] = {
    "shopware-entity-schema": READ_ONLY,
    "shopware-entity-search": READ_ONLY,
    "shopware-entity-read": READ_ONLY,
    "shopware-entity-aggregate": READ_ONLY,
    "shopware-system-config-read": READ_ONLY,
    "shopware-tool-search": READ_ONLY,
    "shopware-toolsets-list": READ_ONLY,
    "shopware-toolset-enable": SESSION_STATE,
    "shopware-entity-upsert": _writes(destructive=True, idempotent=False),
    # Fetches the file from a URL the caller names, which is the open world.
    "shopware-media-upload": _writes(destructive=True, idempotent=False, open_world=True),
    "shopware-entity-delete": _writes(destructive=True, idempotent=True),
    "shopware-order-state": _writes(destructive=True, idempotent=False),
    "shopware-system-config-write": _writes(destructive=True, idempotent=True),
    "shopware-theme-config": _writes(destructive=True, idempotent=True),
}

STORE_EXPECTED: dict[str, JsonObject] = {
    "shopware-store-api-context": READ_ONLY,
    "shopware-tool-search": READ_ONLY,
    "shopware-toolsets-list": READ_ONLY,
    "shopware-toolset-enable": SESSION_STATE,
}

# The deferred tool the search check looks for, and a query core's own test finds it with.
SEARCHED_TOOL = "shopware-entity-delete"
SEARCH_ARGS: JsonObject = {"query": "delete entities", "maxResults": 20}

# A server without the annotations sends none on any core tool. Recorded as a
# skip naming the PR. Delete the tolerance once `shopware.sha` includes
# shopware/shopware#22058: after that, the same symptom is a regression.
PREDATES_ANNOTATIONS = "no core tool carries annotations in tools/list: shopware/shopware#22058 is not on this build"


def expected_for(endpoint: Endpoint) -> dict[str, JsonObject]:
    return ADMIN_EXPECTED if endpoint.name == "admin" else STORE_EXPECTED


def annotation_problem(tool: JsonObject | None, expected: JsonObject) -> str | None:
    """None when the tool declares exactly the expected hints.

    Hints the expectation leaves out must be absent too: the spec's defaults
    for an absent hint are the pessimistic ones, which is what core relies on
    for a read-only tool's `destructiveHint` and `idempotentHint`."""
    if tool is None:
        return "not listed"
    annotations = as_object(tool.get("annotations"))
    if not annotations:
        return "no annotations"
    declared = {hint: annotations[hint] for hint in HINTS if hint in annotations}
    if declared != expected:
        return f"declares {declared}, expected {expected}"
    return None


def search_hit(envelope: JsonObject, name: str) -> JsonObject | None:
    """The tool definition `shopware-tool-search` returned for `name`, if any."""
    for item in as_list(envelope.get("data")):
        tool = as_object(as_object(item).get("tool"))
        if tool.get("name") == name:
            return tool
    return None


def verify_tool_annotations(rep: Reporter, endpoint: Endpoint) -> None:
    name = endpoint.name
    rep.section(f"Tool annotations ({name})")
    session, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(session, endpoint=endpoint)
    listed = {str(t.get("name", "")): as_object(t) for t in mcp_tools_list_all(session, endpoint=endpoint)}
    expected = expected_for(endpoint)
    if not any(as_object(listed.get(tool)).get("annotations") for tool in expected):
        rep.check_skip(f"tool annotations ({name})", PREDATES_ANNOTATIONS)
        return
    for tool, hints in expected.items():
        label = f"tool annotations: {tool} declares its hints in tools/list ({name})"
        problem = annotation_problem(listed.get(tool), hints)
        if problem == "not listed":
            rep.check_skip(label, "not in tools/list with every toolset enabled on this server")
        elif problem:
            rep.check_fail(label, problem)
        else:
            rep.check_pass(label)

    if name != "admin":
        return
    label = f"tool annotations: tool-search returns the hints of {SEARCHED_TOOL} (admin)"
    outcome = read_result(mcp_call(session, "shopware-tool-search", SEARCH_ARGS, endpoint=endpoint))
    if outcome.error:
        rep.check_fail(label, f"shopware-tool-search failed: {outcome.failure[:200]}")
        return
    hit = search_hit(outcome.envelope, SEARCHED_TOOL)
    problem = annotation_problem(hit, ADMIN_EXPECTED[SEARCHED_TOOL])
    if problem:
        rep.check_fail(label, problem)
    else:
        rep.check_pass(label)
