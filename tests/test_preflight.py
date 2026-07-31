"""The preflight's diagnosis table.

Each entry here cost at least one round of manual debugging to identify the
first time. The point of the table is that the second person does not pay that
cost again, so the test asserts the mapping rather than just that it runs.
"""

from eval import preflight


def test_every_probe_tool_is_read_only():
    """A preflight that mutates would be a preflight nobody dares run in CI."""
    import toolclass

    for endpoint, (tool, _args) in preflight.PROBES.items():
        assert toolclass.classify(tool) == "read_only", f"{endpoint} probes with {tool}"


def test_the_missing_header_is_named_as_a_client_bug():
    """It is ours to fix, not the server's — the advice has to say so."""
    advice = preflight.diagnose("UCP-Agent header with a profile URI is required for UCP runtime requests.")

    assert "ucp.agent_header" in advice


def test_plain_http_points_at_the_dotted_localhost_trap():
    """The whole point: `.localhost` looks local and is not accepted, which is
    the single most expensive thing to work out unaided."""
    advice = preflight.diagnose("Plain http is only allowed for local development hosts.")

    assert ".localhost" in advice


def test_the_two_allowlists_are_not_confused_with_each_other():
    """They fail with different messages and need different commands. Conflating
    them sends someone to run the wrong one and conclude the advice is wrong."""
    platform = preflight.diagnose("Platform profile host is not allowed by the current runtime configuration.")
    agent = preflight.diagnose("Agent profile host is not allowed.")

    assert "--platform-allowlist" in platform
    assert "--agent-allowlist" in agent
    assert platform != agent


def test_the_swallowed_internal_error_explains_where_to_actually_look():
    """`internal` carries no information at all — the plugin logs nothing — so
    the diagnosis has to supply the context the error does not."""
    advice = preflight.diagnose("internal: The tool call failed unexpectedly.")

    assert "container" in advice.lower()


def test_an_unknown_error_admits_it_rather_than_guessing():
    """A confidently wrong diagnosis is worse than none: it sends someone to fix
    something that was never broken."""
    advice = preflight.diagnose("some entirely new failure nobody has seen")

    assert "no known diagnosis" in advice.lower()


def test_an_empty_error_does_not_crash_the_table():
    assert preflight.diagnose("")
    assert preflight.diagnose(None)


def test_strict_signing_names_the_default_that_causes_it():
    """signaturePolicy defaults to 'strict', so this fires on any instance nobody
    configured — which is every CI run. The advice has to say it is a default,
    or it reads as a broken tool."""
    advice = preflight.diagnose("signature: Missing signature headers.")

    assert "--signature-policy=off" in advice
    assert "default" in advice.lower()


# ---------------------------------------------------------------------------
# The profile probe: the one cause the error text can never name
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_a_real_profile_is_recognised(monkeypatch):
    monkeypatch.setattr(preflight.requests, "get", lambda *_a, **_k: _Resp(200, {"ucp": {"version": "2026-04-08"}}))

    assert preflight.probe_profile("http://x/.well-known/ucp") == (200, "valid UCP profile")


def test_a_404_profile_is_the_measured_cause_of_internal(monkeypatch):
    """Measured against a live lane: a valid profile answers in ~0.16s, a 404
    fails in ~0.19s with exactly `internal`, and CI failed in 0.14s."""
    monkeypatch.setattr(preflight.requests, "get", lambda *_a, **_k: _Resp(404))

    status, verdict = preflight.probe_profile("http://x/nope")

    assert status == 404 and "not a profile" in verdict


def test_an_error_page_served_as_200_is_not_mistaken_for_a_profile(monkeypatch):
    """Shopware answers an unmatched sales-channel domain with a 200 HTML page,
    so status alone would call that success."""
    monkeypatch.setattr(preflight.requests, "get", lambda *_a, **_k: _Resp(200, None, "<!DOCTYPE html>"))

    assert preflight.probe_profile("http://x")[1].startswith("200 but not JSON")


def test_json_without_a_ucp_key_is_not_a_profile(monkeypatch):
    monkeypatch.setattr(preflight.requests, "get", lambda *_a, **_k: _Resp(200, {"something": "else"}))

    assert "no `ucp` key" in preflight.probe_profile("http://x")[1]


def test_an_unreachable_profile_is_named_as_such(monkeypatch):
    def boom(*_a, **_k):
        raise preflight.requests.ConnectionError("refused")

    monkeypatch.setattr(preflight.requests, "get", boom)
    status, verdict = preflight.probe_profile("http://x")

    assert status is None and "unreachable" in verdict


def test_a_broken_profile_is_reported_as_the_cause(monkeypatch):
    monkeypatch.setattr(preflight, "probe_profile", lambda _u: (404, "not a profile — the server answered"))
    monkeypatch.setattr(preflight.mc, "SW_BASE_URL", "http://shop")

    out = preflight.profile_report("internal: The tool call failed unexpectedly.")

    assert "This is the cause" in out
    assert "UCP_PROFILE_URI" in out


def test_a_working_profile_sends_the_reader_to_the_server_log(monkeypatch):
    """If the profile is fine then `internal` is something else, and the plugin
    logs nothing — saying so beats implying the profile is still suspect."""
    monkeypatch.setattr(preflight, "probe_profile", lambda _u: (200, "valid UCP profile"))
    monkeypatch.setattr(preflight.mc, "SW_BASE_URL", "http://shop")

    out = preflight.profile_report("internal: The tool call failed unexpectedly.")

    assert "This is the cause" not in out
    assert "server log" in out
