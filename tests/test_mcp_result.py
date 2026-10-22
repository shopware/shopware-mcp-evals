"""Reading a tools/call reply, whichever revision and format it arrived in.

The replies here are built the way `McpToolResultRenderer` (shopware/shopware
#20948, #20951) builds them: the legacy text that every revision gets until 6.8,
the `structuredContent` and `resource_link` blocks a 2025-06-18 client gets on
top, and the spec-only text blocks of the `v6.8.0.0` flag. The test that matters
most is the last group: one fixture expectation, graded the same on every shape.
"""

import json

import pytest

import mcp_result as M
from eval import assertions as A
from eval.result_schema import ExpectSpec, JsonObject, McpResponse
from mcp_result import TOOL_RESULT_URI_PREFIX, ToolError, ToolOutcome, from_text, read_result

GENERATED: JsonObject = {"shopware/generatedAt": "2026-10-20T10:00:00+00:00"}
POINTER = f"{TOOL_RESULT_URI_PREFIX}0191.1760958000.abc"


def text(value: object) -> JsonObject:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}


def result(
    *content: JsonObject, is_error: bool = False, structured: object = None, meta: JsonObject | None = None
) -> McpResponse:
    body: JsonObject = {"content": list(content), "isError": is_error}
    if structured is not None:
        body["structuredContent"] = structured
    if meta is not None:
        body["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 99, "result": body}  # pyright: ignore[reportReturnType]


# ---------------------------------------------------------------------------
# Protocol errors
# ---------------------------------------------------------------------------
def test_a_json_rpc_error_is_a_failure_with_its_code_and_no_envelope() -> None:
    outcome = read_result({"error": {"code": -32601, "message": "Tool not found"}})

    assert outcome.shape == "protocol_error"
    assert outcome.failure == "Tool not found"
    assert outcome.error == ToolError("Tool not found", "-32601")
    assert outcome.envelope == {} and outcome.text == ""


def test_an_unlabelled_json_rpc_error_is_still_named() -> None:
    assert read_result({"error": {}}).failure == "unknown error"


# ---------------------------------------------------------------------------
# The legacy envelope: every revision until 6.8
# ---------------------------------------------------------------------------
def test_a_legacy_success_reads_data_and_meta_and_keeps_its_text_verbatim() -> None:
    body = {"success": True, "data": {"total": 7}, "_meta": {"note": "n"}}
    raw = json.dumps(body)

    outcome = read_result(result(text(raw)))

    assert outcome.shape == "legacy" and outcome.ok
    assert outcome.data == {"total": 7}
    assert outcome.meta == {"note": "n"}
    assert outcome.envelope == body
    assert outcome.text == raw, "the model must see exactly what it saw before"


def test_on_2025_06_18_the_legacy_text_arrives_next_to_structured_content() -> None:
    """The renderer keeps the legacy text and adds the structured copy; the text
    still decides how the result is read, so nothing graded on it moves."""
    outcome = read_result(
        result(text({"success": True, "data": {"toolsets": []}}), structured={"toolsets": []}, meta=GENERATED)
    )

    assert outcome.shape == "legacy"
    assert outcome.has_structured and outcome.structured == {"toolsets": []}
    assert outcome.generated_at == GENERATED["shopware/generatedAt"]


def test_a_legacy_failure_carries_the_code_core_now_sends() -> None:
    outcome = read_result(
        result(
            text({"success": False, "error": "Entity no_such_entity is unknown", "code": "invalid_arguments"}),
            is_error=True,
        )
    )

    assert outcome.error == ToolError("Entity no_such_entity is unknown", "invalid_arguments")
    assert outcome.failure == "Entity no_such_entity is unknown", "the message, never the raw JSON"


def test_a_failure_without_a_code_keeps_none_rather_than_the_servers_default() -> None:
    """The server would call it `tool_error`. None is how a grader knows the code
    is absent and the prose is all there is."""
    outcome = read_result(result(text({"success": False, "error": "Cart is empty"})))

    assert outcome.error is not None and outcome.error.code is None


def test_an_in_band_failure_without_is_error_is_still_a_failure() -> None:
    """UCP and the merchant tools: HTTP 200, no flag, `success: false`."""
    outcome = from_text(json.dumps({"success": False}))

    assert not outcome.ok
    assert outcome.failure == "the tool reported success: false"


def test_a_ucp_error_object_is_described_with_its_type_and_violations() -> None:
    outcome = from_text(
        json.dumps(
            {
                "success": False,
                "error": {
                    "type": "validation",
                    "message": 'Validation failed for schema "checkout.create.request"',
                    "violations": ["$.line_items is required"],
                    "severity": "recoverable",
                },
            }
        )
    )

    assert outcome.error is not None
    assert outcome.error.details == {
        "type": "validation",
        "violations": ["$.line_items is required"],
        "severity": "recoverable",
    }
    assert (
        outcome.failure
        == 'validation: Validation failed for schema "checkout.create.request" ($.line_items is required)'
    )


def test_a_code_inside_the_error_object_wins_over_the_envelope_code() -> None:
    outcome = from_text(
        json.dumps({"success": False, "error": {"message": "m", "code": "not_found"}, "code": "tool_error"})
    )

    assert outcome.error == ToolError("m", "not_found")


def test_keys_next_to_the_envelope_are_metadata() -> None:
    """`dryRun` and `preview` of the UCP tools, as McpToolResultParser keeps them."""
    outcome = from_text(json.dumps({"success": True, "data": {}, "dryRun": True, "_meta": {"a": 1}}))

    assert outcome.meta == {"a": 1, "dryRun": True}


def test_is_error_over_a_successful_body_is_still_a_failure() -> None:
    outcome = read_result(result(text({"success": True, "data": []}), is_error=True))

    assert not outcome.ok


# ---------------------------------------------------------------------------
# Offloaded results (#20951)
# ---------------------------------------------------------------------------
def offloaded_meta() -> JsonObject:
    return {
        "resourceUri": POINTER,
        "expiresAt": "2026-10-20T11:00:00+00:00",
        "responseSize": 250_000,
        "note": "Response too large. If you need the full data as-is, fetch it via resources/read.",
    }


def test_before_2025_06_18_the_pointer_is_read_from_the_legacy_meta() -> None:
    outcome = from_text(json.dumps({"success": True, "data": {"preview": []}, "_meta": offloaded_meta()}))

    (link,) = outcome.links
    assert (link.uri, link.source, link.size) == (POINTER, "meta", 250_000)
    assert outcome.summary is not None and outcome.summary.startswith("Response too large")
    assert outcome.expires_at == "2026-10-20T11:00:00+00:00"


def test_from_2025_06_18_the_resource_link_block_is_the_link() -> None:
    """The same pointer arrives twice — in the legacy meta and as the block — and
    is one link, credited to the block."""
    outcome = read_result(
        result(
            text({"success": True, "data": {}, "_meta": offloaded_meta()}),
            {
                "type": "resource_link",
                "uri": POINTER,
                "name": "tool-result",
                "mimeType": "application/json",
                "size": 250_000,
            },
            meta={**GENERATED, "shopware/expiresAt": "2026-10-20T11:00:00+00:00"},
        )
    )

    (link,) = outcome.links
    assert (link.uri, link.source, link.mime_type) == (POINTER, "resource_link", "application/json")
    assert outcome.text == json.dumps({"success": True, "data": {}, "_meta": offloaded_meta()}), "a link is no text"


def test_an_extensions_own_resource_uri_is_not_a_stored_result() -> None:
    outcome = from_text(json.dumps({"success": True, "data": {}, "_meta": {"resourceUri": "app://thing/1"}}))

    assert outcome.links == ()
    assert outcome.summary is None


# ---------------------------------------------------------------------------
# The spec-only format (`v6.8.0.0`)
# ---------------------------------------------------------------------------
def test_a_spec_success_is_read_back_into_the_envelope_the_callers_know() -> None:
    outcome = read_result(
        result(
            text({"total": 7}),
            text({"_meta": {"note": "n"}}),
            structured={"total": 7},
            meta={**GENERATED, "shopware/note": "n"},
        )
    )

    assert outcome.shape == "spec"
    assert outcome.data == {"total": 7}
    assert outcome.meta == {"note": "n"}
    assert outcome.envelope == {"success": True, "data": {"total": 7}, "_meta": {"note": "n"}}


def test_a_spec_list_is_the_text_not_the_wrapped_structured_copy() -> None:
    """On the handshake era a list arrives as `{"result": [...]}` in
    structuredContent. The text block is the unwrapped original."""
    outcome = read_result(result(text([1, 2]), structured={"result": [1, 2]}, meta=GENERATED))

    assert outcome.data == [1, 2]


def test_spec_metadata_falls_back_to_the_prefixed_result_meta() -> None:
    outcome = read_result(result(text({}), meta={**GENERATED, "shopware/dryRun": True}))

    assert outcome.meta == {"dryRun": True}, "generatedAt is the renderer's stamp, not the tool's metadata"


def test_a_spec_offload_carries_data_summary_and_link() -> None:
    outcome = read_result(
        result(
            text({"preview": []}),
            text("Response too large."),
            text({"_meta": {"resourceUri": POINTER}}),
            {"type": "resource_link", "uri": POINTER, "name": "tool-result"},
            meta=GENERATED,
        )
    )

    assert outcome.data == {"preview": []}
    assert outcome.summary == "Response too large."
    assert [link.uri for link in outcome.links] == [POINTER]


def test_a_spec_result_without_data_starts_with_its_summary() -> None:
    outcome = read_result(result(text("Stored behind a link."), meta=GENERATED))

    assert outcome.data is None
    assert outcome.summary == "Stored behind a link."


def test_a_spec_failure_reads_code_and_details_from_the_structured_error() -> None:
    outcome = read_result(
        result(
            text("Order 0191 was not found."),
            text({"details": {"id": "0191"}}),
            is_error=True,
            structured={
                "error": {"code": "not_found", "message": "Order 0191 was not found.", "details": {"id": "0191"}}
            },
            meta=GENERATED,
        )
    )

    assert outcome.error == ToolError("Order 0191 was not found.", "not_found", {"id": "0191"})
    assert outcome.envelope == {
        "success": False,
        "error": {"message": "Order 0191 was not found.", "id": "0191"},
        "code": "not_found",
    }


def test_a_spec_failure_before_2025_06_18_has_details_but_no_code() -> None:
    """No structuredContent below 2025-06-18, and the spec text never carries the
    code — so an old client on 6.8 is back to grading prose."""
    outcome = read_result(result(text("Nope."), text({"details": {"field": "id"}}), is_error=True, meta=GENERATED))

    assert outcome.error == ToolError("Nope.", None, {"field": "id"})


# ---------------------------------------------------------------------------
# Plain text: a tool returning its own CallToolResult
# ---------------------------------------------------------------------------
def test_plain_text_is_data_with_no_envelope() -> None:
    outcome = read_result(result(text("Available files: a.log, b.log")))

    assert outcome.shape == "plain" and outcome.ok
    assert outcome.data == "Available files: a.log, b.log"
    assert outcome.envelope == {} and outcome.payload is None


def test_a_json_object_without_success_is_its_own_envelope() -> None:
    """What older fakes and some extension tools send; `data` still resolves."""
    outcome = from_text(json.dumps({"data": [1]}))

    assert outcome.envelope == {"data": [1]}
    assert outcome.payload == {"data": [1]}


def test_plain_text_flagged_is_error_is_a_failure() -> None:
    """This used to pass the functional suite: no JSON-RPC error, no envelope to
    find `success: false` in, and some content."""
    assert (
        read_result(result(text("Error while executing tool"), is_error=True)).failure == "Error while executing tool"
    )
    assert read_result(result(is_error=True)).failure == "tool error"


def test_text_joins_every_text_block_in_order() -> None:
    outcome = read_result(result(text("a"), {"type": "image", "data": "..."}, text("b")))

    assert outcome.texts == ("a", "b") and outcome.text == "a\nb"


def test_a_block_without_a_type_is_text() -> None:
    assert read_result({"result": {"content": [{"text": "implicit"}]}}).text == "implicit"


def test_an_empty_reply_is_empty() -> None:
    outcome = read_result({})

    assert outcome.texts == () and outcome.ok and outcome.payload is None


# ---------------------------------------------------------------------------
# One expectation, every shape
# ---------------------------------------------------------------------------
SAME_ANSWER = {
    "legacy": result(text({"success": True, "data": {"total": 7, "elements": [1, 2]}})),
    "legacy, 2025-06-18": result(
        text({"success": True, "data": {"total": 7, "elements": [1, 2]}}),
        structured={"total": 7, "elements": [1, 2]},
        meta=GENERATED,
    ),
    "spec": result(text({"total": 7, "elements": [1, 2]}), structured={"total": 7, "elements": [1, 2]}, meta=GENERATED),
}

SAME_FAILURE = {
    "legacy": result(
        text({"success": False, "error": "Order 0191 was not found.", "code": "not_found"}), is_error=True
    ),
    "spec": result(
        text("Order 0191 was not found."),
        is_error=True,
        structured={"error": {"code": "not_found", "message": "Order 0191 was not found."}},
        meta=GENERATED,
    ),
    "spec, 2024-11-05": result(text("Order 0191 was not found."), is_error=True, meta=GENERATED),
}


@pytest.mark.parametrize("shape", SAME_ANSWER)
def test_a_fixture_expectation_grades_the_same_on_every_shape(shape: str) -> None:
    """Fixtures address the payload as `data.…`. That path has to keep meaning
    the data when 6.8 stops sending the envelope around it."""
    spec: ExpectSpec = {"tier": "data", "has_keys": ["data.total"], "min_items": {"path": "data.elements", "n": 2}}

    assert A.check(spec, read_result(SAME_ANSWER[shape])) == (True, None)


@pytest.mark.parametrize("shape", SAME_FAILURE)
def test_a_not_found_grades_the_same_on_every_shape(shape: str) -> None:
    outcome = read_result(SAME_FAILURE[shape])

    assert A.check("accepted", outcome) == (True, None)
    assert A.check("data", outcome) == (False, "not_found")


def test_a_json_rpc_error_without_a_numeric_code_has_none() -> None:
    assert read_result({"error": {"message": "x"}}).error == ToolError("x")
    assert read_result({"error": {"code": True, "message": "x"}}).error == ToolError("x")


# --- the format per revision (shopware/shopware#21844) -----------------------------


def _text(text: str) -> ToolOutcome:
    return read_result({"result": {"content": [{"type": "text", "text": text}], "_meta": GENERATED}})


LEGACY_REPLY = '{"success": true, "data": []}'


def test_the_format_follows_the_revision() -> None:
    assert [M.format_for(r) for r in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")] == [
        "legacy",
        "legacy",
        "spec",
        "spec",
        "spec",
    ]


def test_the_verdict_tells_the_generations_apart() -> None:
    legacy, spec = _text(LEGACY_REPLY), _text("[]")
    refused = read_result({"error": {"code": -32602, "message": "nope"}})

    assert M.format_verdict([("2024-11-05", legacy), ("2025-06-18", spec), ("2025-06-18", refused)]) == "by_revision"
    assert M.format_verdict([("2024-11-05", legacy), ("2025-06-18", legacy)]) == "one_format"
    assert M.format_verdict([("2024-11-05", spec), ("2025-06-18", spec)]) == "one_format"
    assert M.format_verdict([("2024-11-05", spec), ("2025-06-18", legacy)]) == "mixed"


def test_the_verdict_names_a_run_without_a_rendered_reply() -> None:
    refused = read_result({"error": {"code": -32602, "message": "nope"}})

    assert M.format_verdict([]) == "no_format"
    assert M.format_verdict([("2024-11-05", refused), ("2025-06-18", refused)]) == "no_format"


def _structured(data: object, structured: object, meta: dict[str, object], summary: str | None = None) -> ToolOutcome:
    texts = [json.dumps(data), *([summary] if summary else []), json.dumps({"_meta": meta})]
    return read_result(
        {
            "result": {
                "content": [{"type": "text", "text": t} for t in texts],
                "structuredContent": structured,
                "_meta": {"shopware/generatedAt": "2026-10-22T10:00:00+00:00"},
            }
        }
    )


def test_model_meta_drops_the_bookkeeping_and_adds_the_summary() -> None:
    outcome = _structured([1], {"result": [1]}, {"dryRun": True, "responseSize": 9, "resourceUri": "x"}, "one row")

    assert outcome.model_meta == {"dryRun": True, "summary": "one row"}


def test_structured_meta_and_data_split_the_reserved_key() -> None:
    wrapped = _structured([1], {"result": [1], "_meta": {"dryRun": True}}, {"dryRun": True})
    plain = _structured({"a": 1}, {"a": 1}, {})
    own = _structured({"_meta": 1, "rows": []}, {"_meta": 1, "rows": []}, {})

    assert (wrapped.structured_meta, wrapped.structured_data) == ({"dryRun": True}, {"result": [1]})
    assert (plain.structured_meta, plain.structured_data) == (None, {"a": 1})
    assert own.data_has_meta_key and (own.structured_meta, own.structured_data) == (None, {"_meta": 1, "rows": []})
