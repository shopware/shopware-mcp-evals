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
