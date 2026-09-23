"""The allowlist matrix: what each kind of principal reaches over MCP.

Driven through a fake server that enforces an allowlist per access key, so the
tests can break it the ways a real one has broken or could: fail-open for an
unset allowlist (#20600 reverted), an "All capabilities" integration that is
listed no toolsets (the report that started this), a snapshot that misses a
tool, a one-tool grant that leaks a sibling.
"""

import json
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from eval.result_schema import JsonObject, McpResponse, ToolDef, Toolset
from functional import runner as R
from functional.principals import PROBE_TOOL, Principal, ProvisioningFailed
from functional.reporting import Reporter
from mcp_client import ALL_TOOLSETS, META_TOOLS, Endpoint, admin_endpoint
from tests.stubs import const, never, raiser

CONTROL = "shopware-system-config-read"
CATALOGUE = {"entity": {PROBE_TOOL, "shopware-entity-read"}, "system-config": {CONTROL}}
EVERY_TOOL = {PROBE_TOOL, "shopware-entity-read", CONTROL}
RESOURCES = ["shopware://entities"]
PROMPTS = ["shopware-context"]

PRINCIPALS = [
    Principal("mcp-evals-abc123-integration-unset", "UNSET", "s", "blocked"),
    Principal("mcp-evals-abc123-integration-all", "ALL", "s", "all"),
    Principal("mcp-evals-abc123-integration-one-tool", "ONE", "s", "partial"),
]
SUITE = admin_endpoint("SUITE", "s", base_url="http://shop.example")

# One check per surface: default surface, toolsets-list, toolset-enable,
# ?toolsets=all, resources/list, prompts/list, the probe call — plus the control
# call for the one-tool principal.
CHECKS = 7 + 7 + 8


def text_resp(payload: JsonObject) -> McpResponse:
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


class FakeServer:
    """An MCP server that filters every surface by the caller's allowlist."""

    def __init__(self) -> None:
        self.grants: dict[str, set[str]] = {"SUITE": EVERY_TOOL, "UNSET": set(), "ALL": EVERY_TOOL, "ONE": {PROBE_TOOL}}
        self.refuse_init: set[str] = set()
        # The reported bug: a principal whose toolsets-list is empty whatever it holds.
        self.no_toolsets_for: set[str] = set()

    @staticmethod
    def key(endpoint: Endpoint | None) -> str:
        assert endpoint is not None
        return endpoint.auth_headers["sw-access-key"]

    def allowed(self, endpoint: Endpoint | None) -> set[str]:
        return self.grants[self.key(endpoint)]

    def init(self, endpoint: Endpoint | None = None) -> tuple[str, str]:
        if self.key(endpoint) in self.refuse_init:
            raise RuntimeError("invalid credentials")
        return "session", ""

    def tools_list(self, _session: str, endpoint: Endpoint | None = None) -> list[ToolDef]:
        assert endpoint is not None
        pinned = self.allowed(endpoint) if ALL_TOOLSETS in endpoint.toolsets else set[str]()
        return [ToolDef(name=n) for n in sorted(META_TOOLS | pinned)]

    def toolsets_list(self, _session: str, endpoint: Endpoint | None = None) -> list[Toolset]:
        if self.key(endpoint) in self.no_toolsets_for:
            return []
        allowed = self.allowed(endpoint)
        return [Toolset(name=n, tools=sorted(t & allowed)) for n, t in CATALOGUE.items() if t & allowed]

    def enable(self, _session: str, toolset: str, endpoint: Endpoint | None = None) -> McpResponse:
        if CATALOGUE.get(toolset, set()) & self.allowed(endpoint):
            return text_resp({"success": True})
        return text_resp({"success": False, "error": f'Unknown MCP toolset "{toolset}".'})

    def list_names(self, _session: str, method: str, endpoint: Endpoint | None = None) -> list[str]:
        if self.allowed(endpoint) >= EVERY_TOOL:
            return RESOURCES if method == "resources/list" else PROMPTS
        return []

    def call(self, _session: str, tool: str, _args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        if tool in self.allowed(endpoint):
            return text_resp({"success": True, "data": []})
        return {"error": {"code": -32602, "message": f"Tool {tool} is not enabled in your MCP allowlist."}}


def wire(monkeypatch: pytest.MonkeyPatch, fake: FakeServer, principals: list[Principal] | None = None) -> list[str]:
    """Point the runner at `fake`; returns a log that records provisioning teardown."""
    log: list[str] = []

    @contextmanager
    def provisioned(_api: object) -> Generator[list[Principal]]:
        try:
            yield PRINCIPALS if principals is None else principals
        finally:
            log.append("deleted")

    monkeypatch.setattr(R, "mcp_init", fake.init)
    monkeypatch.setattr(R, "mcp_tools_list_all", fake.tools_list)
    monkeypatch.setattr(R, "mcp_toolsets_list", fake.toolsets_list)
    monkeypatch.setattr(R, "enable_toolset", fake.enable)
    monkeypatch.setattr(R, "mcp_list_names", fake.list_names)
    monkeypatch.setattr(R, "mcp_call", fake.call)
    monkeypatch.setattr(R, "AdminApi", const(object()))
    monkeypatch.setattr(R, "provisioned", provisioned)
    return log


def run(fake: FakeServer, monkeypatch: pytest.MonkeyPatch, principals: list[Principal] | None = None) -> Reporter:
    _ = wire(monkeypatch, fake, principals)
    rep = Reporter("admin", color=False)
    R.verify_allowlist_matrix(rep, SUITE, provision=True)
    return rep


def failures(rep: Reporter) -> list[str]:
    return [f"{r['label']}: {r.get('error', '')}" for r in rep.records if r["status"] == "fail"]


def test_a_server_that_enforces_the_allowlist_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer()
    log = wire(monkeypatch, fake)
    rep = Reporter("admin", color=False)

    R.verify_allowlist_matrix(rep, SUITE, provision=True)

    assert failures(rep) == []
    assert rep.passed == CHECKS
    assert log == ["deleted"]


def test_fail_open_for_an_unset_allowlist_is_caught_on_every_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """#20600 reverted. Only the default surface still looks right, because it
    is the meta-tools for everyone."""
    fake = FakeServer()
    fake.grants["UNSET"] = EVERY_TOOL

    fails = failures(run(fake, monkeypatch))

    assert len(fails) == 6
    assert all("integration-unset" in f for f in fails)


def test_an_all_capabilities_integration_listed_no_toolsets_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report: "All capabilities" on, and shopware-toolsets-list answers []."""
    fake = FakeServer()
    fake.no_toolsets_for = {"ALL"}

    fails = failures(run(fake, monkeypatch))

    assert len(fails) == 1
    assert "integration-all" in fails[0]
    assert "is listed every toolset" in fails[0]
    assert "missing entity" in fails[0]


def test_an_all_selection_that_misses_a_tool_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """What "All" saves is a snapshot. A tool it did not capture must show up by
    name, not as a count."""
    fake = FakeServer()
    fake.grants["ALL"] = EVERY_TOOL - {CONTROL}

    fails = " ".join(failures(run(fake, monkeypatch)))

    assert f"missing {CONTROL}" in fails


def test_a_one_tool_grant_that_leaks_a_sibling_toolset_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer()
    fake.grants["ONE"] = {PROBE_TOOL, CONTROL}

    fails = failures(run(fake, monkeypatch))

    assert fails
    assert all("integration-one-tool" in f for f in fails)
    assert any(f"is refused {CONTROL}" in f for f in fails)


def test_a_refusal_has_to_name_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """An argument-validation error is also an error. Counting it as a refusal
    would pass a server that enforces nothing, as long as it is strict about
    arguments."""
    fake = FakeServer()
    inner = fake.call

    def call(_session: str, tool: str, _args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        if fake.key(endpoint) == "UNSET":
            return {"result": {"isError": True, "content": [{"type": "text", "text": "entity is required"}]}}
        return inner(_session, tool, _args, endpoint)

    fake.call = call

    fails = failures(run(fake, monkeypatch))

    assert len(fails) == 1
    assert f"is refused {PROBE_TOOL}" in fails[0]


def test_a_blocked_principal_refused_a_session_passes_and_nothing_else_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeServer()
    fake.refuse_init = {"UNSET", "ALL"}

    rep = run(fake, monkeypatch)
    fails = failures(rep)

    assert len(fails) == 1
    assert "integration-all" in fails[0]
    assert "initialize failed" in fails[0]
    assert any("integration-unset: opens a session" in str(r["label"]) for r in rep.records)


def test_a_transport_error_aborts_one_principal_and_the_rest_still_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer()
    inner = fake.list_names

    def list_names(_session: str, method: str, endpoint: Endpoint | None = None) -> list[str]:
        if fake.key(endpoint) == "UNSET":
            raise RuntimeError("resources/list failed: boom")
        return inner(_session, method, endpoint)

    fake.list_names = list_names

    rep = run(fake, monkeypatch)
    fails = failures(rep)

    assert len(fails) == 1
    assert "aborted: resources/list failed: boom" in fails[0]
    assert rep.passed == CHECKS - 7 + 4, "the blocked principal stops after its first four checks"


def test_the_matrix_is_skipped_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """It creates integrations, users and a role. A developer's shop must not
    get them without asking."""
    monkeypatch.setattr(R, "provisioned", never("provisioned without --provision-principals"))
    monkeypatch.setattr(R, "load_catalogue", never("read the catalogue without --provision-principals"))
    rep = Reporter("admin", color=False)

    R.verify_allowlist_matrix(rep, SUITE, provision=False)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)


def test_a_reference_principal_that_reaches_nothing_fails_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every comparison is against the suite's own reach. If that is empty,
    "reaches as much as the administrator" passes for a principal that reaches
    nothing."""
    fake = FakeServer()
    fake.grants["SUITE"] = set()
    _ = wire(monkeypatch, fake)
    monkeypatch.setattr(R, "provisioned", never("provisioned against an empty reference"))
    rep = Reporter("admin", color=False)

    R.verify_allowlist_matrix(rep, SUITE, provision=True)

    assert failures(rep) == [
        f"allowlist matrix: the suite's own principal reaches no {PROBE_TOOL}, so nothing can be compared"
    ]


def test_an_unreadable_reference_catalogue_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "load_catalogue", raiser(RuntimeError("tools/list pagination did not terminate")))
    rep = Reporter("admin", color=False)

    R.verify_allowlist_matrix(rep, SUITE, provision=True)

    assert rep.failed == 1
    assert "could not read the reference catalogue" in failures(rep)[0]


def test_a_provisioning_failure_fails_the_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeServer()
    _ = wire(monkeypatch, fake)

    failure = ProvisioningFailed("POST /api/user answered HTTP 400: localeId is required")
    monkeypatch.setattr(R, "provisioned", raiser(failure))
    rep = Reporter("admin", color=False)

    R.verify_allowlist_matrix(rep, SUITE, provision=True)

    assert failures(rep) == [
        "allowlist matrix: could not provision the principals: POST /api/user answered HTTP 400: localeId is required"
    ]


def test_the_principal_endpoint_targets_the_suites_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the process-wide SW_BASE_URL: a suite pointed at another instance
    must not check principals it created there against this one."""
    fake = FakeServer()
    seen: list[str] = []
    inner = fake.init

    def init(endpoint: Endpoint | None = None) -> tuple[str, str]:
        assert endpoint is not None
        seen.append(endpoint.base_url)
        return inner(endpoint)

    fake.init = init
    _ = run(fake, monkeypatch)

    assert seen
    assert set(seen) == {"http://shop.example"}
