#!/usr/bin/env python3
"""What a tools/call reply says, whichever protocol revision and format it came in.

A Shopware tool result has three shapes today, and which one arrives depends on
the server and the revision the client negotiated, not on the tool:

  legacy   one text block holding `{"success": …, "data": …, "_meta": …}`. Since
           shopware/shopware#20948 a failure also carries a stable `code` next to
           `error`.
  spec     the MCP format: the first text block is the plain data as JSON (or
           the error message), followed by the summary, `{"details": …}` and
           `{"_meta": …}` as blocks of their own; the metadata also arrives in the
           result `_meta` under a `shopware/` prefix.
  plain    a string that is no envelope at all, which the server passes through
           untouched (a tool returning its own `CallToolResult`).

Since shopware/shopware#21844 the revision picks between the first two
(`format_for`): legacy before 2025-06-18, spec from it on, with
`structuredContent` (carrying the model's metadata under `_meta`) and, for an
offloaded result, a `resource_link` block instead of `_meta.resourceUri`.
Before #21844 every revision got legacy, or spec under the `v6.8.0.0` flag, and
2025-06-18 added `structuredContent` and the block on top of either.
`format_verdict` tells the two generations apart.

Every reader in this repo used to `json.loads` the first text block and ask it
for `success`, `data` and `_meta` — exactly the keys 6.8 removes. `read_result()` is the
one place that knows the shapes, so a format change is a change here and nowhere
else. `ToolOutcome.envelope` gives callers the legacy-shaped view they and the
fixtures' `has_keys` paths (`data.elements`) are written against, whatever
actually arrived.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal, cast

from eval.result_schema import JsonObject, McpResponse, as_list, as_object

# `McpToolResultPointer::URI_PREFIX`. Only a pointer with this prefix is the
# server's offloaded result; any other `resourceUri` an extension sends is plain
# metadata, as `McpToolResultParser::success()` treats it.
TOOL_RESULT_URI_PREFIX = "shopware://tool-result/"

# `McpToolResultRenderer::META_PREFIX`.
META_PREFIX = "shopware/"

# The legacy envelope's own keys. Anything next to them (`dryRun`, `preview` of
# the UCP tools) is metadata, mirroring `McpToolResultParser::ENVELOPE_KEYS`.
_ENVELOPE_KEYS = frozenset({"success", "data", "_meta", "error", "code"})

type Shape = Literal["protocol_error", "legacy", "spec", "plain"]

# shopware/shopware#21844: the negotiated revision picks the format, not a
# feature flag. `McpToolResultCapabilities::forRevision()`.
SPEC_FORMAT_SINCE = "2025-06-18"

type ResultFormat = Literal["legacy", "spec"]

# `McpToolResultRenderer::STRUCTURED_META_KEY` and `INTERNAL_META_KEYS`: since
# shopware/shopware#21844 (a5c30a4) `structuredContent` carries the metadata the
# model needs under `_meta`, without the client's bookkeeping.
STRUCTURED_META_KEY = "_meta"
_INTERNAL_META_KEYS = frozenset({"responseSize", "resourceUri"})


def format_for(revision: str) -> ResultFormat:
    """The format a server since shopware/shopware#21844 sends on `revision`:
    the MCP format from 2025-06-18 on, the deprecated legacy envelope before."""
    return "spec" if revision >= SPEC_FORMAT_SINCE else "legacy"


@dataclass(frozen=True)
class ToolError:
    """A failed call, in the server's terms.

    `code` is None when the server sent none — a plugin tool that predates the
    codes, or a server older than #20948. A protocol error carries its JSON-RPC
    code as a string (`-32602`), which is a code like any other. Callers that grade on
    the code fall back to the message then, and that None is how they know to.
    """

    message: str
    code: str | None = None
    details: JsonObject = field(default_factory=dict)

    def describe(self) -> str:
        """One line for a report or a model, in the form graders match on.

        `type` is the classification UCP already made, kept in front so the
        anchored `not_found:` matcher in eval.assertions still sees it.
        `violations` is where a UCP schema error's actual answer is: the message
        says only 'Validation failed for schema "checkout.create.request"',
        and `["$.line_items is required"]` sits next to it.
        """
        kind = self.details.get("type")
        text = f"{kind!s}: {self.message}".strip(": ").strip() if kind else self.message
        violations = as_list(self.details.get("violations"))
        if violations:
            text += f" ({'; '.join(str(v) for v in violations)})"
        return text


@dataclass(frozen=True)
class ResultLink:
    """A pointer to a stored result. `source` says how it arrived: as a
    `resource_link` block (2025-06-18+) or as `_meta.resourceUri` in the text."""

    uri: str
    source: Literal["resource_link", "meta"]
    name: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class ToolOutcome:
    """A tools/call reply, read once.

    `texts` are all text blocks in order, and `text` joins them: that is what a
    client hands the model. On the legacy format there is exactly one block, so
    `text` is byte-identical to what `mcp_result_text` returned before.
    """

    shape: Shape
    data: object = None
    error: ToolError | None = None
    meta: JsonObject = field(default_factory=dict)
    summary: str | None = None
    links: tuple[ResultLink, ...] = ()
    texts: tuple[str, ...] = ()
    structured: object = None
    has_structured: bool = False
    is_error_flag: bool = False
    result_meta: JsonObject = field(default_factory=dict)
    envelope: JsonObject = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    @property
    def failure(self) -> str:
        """The error as one line, or "" for a call that worked."""
        return self.error.describe() if self.error else ""

    @property
    def payload(self) -> object:
        """What predicates walk: the envelope for an enveloped result, else the
        decoded text, else None. Exists so `has_keys: [data.elements]` means the
        same thing on every format."""
        if self.shape in ("legacy", "spec"):
            return self.envelope
        return _decode(self.text)

    @property
    def expires_at(self) -> str | None:
        value = self.result_meta.get(META_PREFIX + "expiresAt") or self.meta.get("expiresAt")
        return value if isinstance(value, str) else None

    @property
    def generated_at(self) -> str | None:
        value = self.result_meta.get(META_PREFIX + "generatedAt")
        return value if isinstance(value, str) else None

    @property
    def model_meta(self) -> JsonObject:
        """The metadata a model has to see: the result's metadata without the
        client's bookkeeping, plus the summary. What `structuredContent._meta`
        must hold since shopware/shopware#21844 (`McpToolResultRenderer::modelMeta()`)."""
        meta = {key: value for key, value in self.meta.items() if key not in _INTERNAL_META_KEYS}
        if self.summary and "summary" not in meta:
            meta["summary"] = self.summary
        return meta

    @property
    def data_has_meta_key(self) -> bool:
        """The data is an object with a `_meta` key of its own, which the server
        then leaves alone rather than overwriting it with the metadata."""
        return STRUCTURED_META_KEY in as_object(self.data)

    @property
    def structured_meta(self) -> JsonObject | None:
        """`structuredContent._meta`, or None when it carries no such key or it
        is the data's own."""
        structured = self.structured
        if not isinstance(structured, dict) or STRUCTURED_META_KEY not in structured or self.data_has_meta_key:
            return None
        return as_object(cast(object, structured[STRUCTURED_META_KEY]))

    @property
    def structured_data(self) -> object:
        """`structuredContent` without the reserved `_meta` key: the data, or
        `{"result": …}` for data that is no object."""
        structured = self.structured
        if self.structured_meta is None or not isinstance(structured, dict):
            return structured
        return {key: value for key, value in cast(JsonObject, structured).items() if key != STRUCTURED_META_KEY}


def _decode(text: str | None) -> object:
    if not text:
        return None
    try:
        return cast(object, json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _links(content: list[object], meta: JsonObject) -> tuple[ResultLink, ...]:
    links: list[ResultLink] = []
    for raw in content:
        block = as_object(raw)
        if block.get("type") == "resource_link" and isinstance(block.get("uri"), str):
            size = block.get("size")
            links.append(
                ResultLink(
                    uri=str(block["uri"]),
                    source="resource_link",
                    name=_str(block.get("name")),
                    description=_str(block.get("description")),
                    mime_type=_str(block.get("mimeType")),
                    size=size if isinstance(size, int) else None,
                )
            )
    uri = meta.get("resourceUri")
    if isinstance(uri, str) and uri.startswith(TOOL_RESULT_URI_PREFIX) and uri not in {link.uri for link in links}:
        size = meta.get("responseSize")
        links.append(
            ResultLink(
                uri=uri,
                source="meta",
                name="tool-result",
                description=_str(meta.get("note")),
                mime_type="application/json",
                size=size if isinstance(size, int) else None,
            )
        )
    return tuple(links)


def _legacy_error(envelope: JsonObject) -> ToolError:
    """`McpToolResultParser::error()`, except that an absent code stays None
    instead of becoming `tool_error`: the server's default would hide exactly the
    tools that still send no code."""
    raw = envelope.get("error")
    code = _str(envelope.get("code"))
    if isinstance(raw, str):
        return ToolError(raw, code)
    if raw_object := as_object(raw):
        details = dict(raw_object)
        message = _str(details.pop("message", None)) or "The tool call failed."
        code = _str(details.pop("code", None)) or code
        return ToolError(message, code, details)
    return ToolError("the tool reported success: false", code)


def _legacy(envelope: JsonObject) -> tuple[object, ToolError | None, JsonObject]:
    meta = dict(as_object(envelope.get("_meta")))
    for key, value in envelope.items():
        if key not in _ENVELOPE_KEYS:
            meta.setdefault(key, value)
    if envelope.get("success") is False:
        return None, _legacy_error(envelope), meta
    return envelope.get("data"), None, meta


def _structural(text: str, key: str) -> JsonObject | None:
    """The `{"<key>": …}` block of the spec format, or None if `text` is not one."""
    decoded = as_object(_decode(text))
    return as_object(decoded.get(key)) if len(decoded) == 1 and key in decoded else None


def _spec(
    texts: tuple[str, ...], structured: object, result_meta: JsonObject, is_error: bool
) -> tuple[object, ToolError | None, JsonObject, str | None]:
    """Reads `McpToolResultRenderer::specTexts()` back."""
    meta: JsonObject = {}
    details: JsonObject = {}
    rest: list[str] = []
    for text in texts:
        if (block := _structural(text, "_meta")) is not None:
            meta = block
        elif (block := _structural(text, "details")) is not None:
            details = block
        else:
            rest.append(text)
    if not meta:
        meta = {
            key[len(META_PREFIX) :]: value
            for key, value in result_meta.items()
            if key.startswith(META_PREFIX) and key not in (META_PREFIX + "generatedAt", META_PREFIX + "expiresAt")
        }
    if is_error:
        error = as_object(as_object(structured).get("error"))
        message = _str(error.get("message")) or (rest[0] if rest else "tool error")
        return None, ToolError(message, _str(error.get("code")), as_object(error.get("details")) or details), meta, None
    # The data comes first, as JSON. A result stored behind a link may have none,
    # and then the first block is already the summary — prose, not JSON.
    data: object = None
    if rest:
        try:
            data = cast(object, json.loads(rest[0]))
            rest = rest[1:]
        except json.JSONDecodeError:
            pass
    return data, None, meta, (rest[0] if rest else None)


def _envelope(data: object, error: ToolError | None, meta: JsonObject) -> JsonObject:
    """The legacy envelope a spec-format result would have had."""
    if error is not None:
        envelope: JsonObject = {
            "success": False,
            "error": {"message": error.message, **error.details} if error.details else error.message,
        }
        if error.code is not None:
            envelope["code"] = error.code
        return envelope
    envelope = {"success": True, "data": data}
    if meta:
        envelope["_meta"] = meta
    return envelope


def read_result(resp: McpResponse) -> ToolOutcome:
    """Read one tools/call reply into a ToolOutcome."""
    if "error" in resp:
        error = as_object(resp.get("error"))
        message = str(error.get("message") or "unknown error")
        rpc_code = error.get("code")
        code = str(rpc_code) if isinstance(rpc_code, int) and not isinstance(rpc_code, bool) else None
        return ToolOutcome(shape="protocol_error", error=ToolError(message, code))

    result = as_object(resp.get("result"))
    content = as_list(result.get("content"))
    texts = tuple(str(as_object(b).get("text") or "") for b in content if as_object(b).get("type", "text") == "text")
    result_meta = as_object(result.get("_meta"))
    is_error = result.get("isError") is True
    has_structured = "structuredContent" in result and result.get("structuredContent") is not None
    structured = result.get("structuredContent")
    base = ToolOutcome(
        shape="plain",
        texts=texts,
        structured=structured,
        has_structured=has_structured,
        is_error_flag=is_error,
        result_meta=result_meta,
    )

    first = as_object(_decode(texts[0])) if texts else {}
    if isinstance(first.get("success"), bool):
        data, error, meta = _legacy(first)
        if error is None and is_error:
            error = ToolError(texts[0])
        links = _links(content, meta)
        # The offload note, which the server's own parser promotes to the summary.
        summary = _str(meta.get("note")) if any(link.source == "meta" for link in links) else None
        return replace(
            base, shape="legacy", data=data, error=error, meta=meta, summary=summary, links=links, envelope=first
        )

    # The renderer stamps every result it builds; a pass-through string gets no
    # stamp. That is the only reliable line between the spec format and plain text.
    if META_PREFIX + "generatedAt" in result_meta:
        data, error, meta, summary = _spec(texts, structured, result_meta, is_error)
        return replace(
            base,
            shape="spec",
            data=data,
            error=error,
            meta=meta,
            summary=summary,
            links=_links(content, meta),
            envelope=_envelope(data, error, meta),
        )

    text = "\n".join(texts)
    decoded = _decode(text)
    return replace(
        base,
        data=decoded if decoded is not None else (text or None),
        error=ToolError(text or "tool error") if is_error else None,
        links=_links(content, {}),
        envelope=as_object(decoded),
    )


def from_text(text: str, is_error: bool = False) -> ToolOutcome:
    """The outcome of a reply carrying one text block and nothing else: what every
    server answered before shopware/shopware#20948, and what a test usually means."""
    return read_result({"result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


type FormatVerdict = Literal["by_revision", "one_format", "mixed", "no_format"]


def format_verdict(replies: Iterable[tuple[str, ToolOutcome]]) -> FormatVerdict:
    """Whether a server picks the result format by revision, from replies on several.

    `by_revision`: every reply is in the format `format_for` names, which is
    shopware/shopware#21844. `one_format`: one format on every revision, which
    is a server from before it (the legacy envelope everywhere, or the
    `v6.8.0.0` flag's spec format everywhere). `mixed`: neither, which no
    server is meant to send. Protocol errors and plain text say nothing about
    the format and are left out. `no_format`: nothing was left, so nothing
    shows which generation the server is. Counting that as `by_revision` would
    pass a check on a server that answered every probe with an error.
    """
    rendered = [(revision, outcome.shape) for revision, outcome in replies if outcome.shape in ("legacy", "spec")]
    if not rendered:
        return "no_format"
    if all(shape == format_for(revision) for revision, shape in rendered):
        return "by_revision"
    if len({shape for _, shape in rendered}) == 1:
        return "one_format"
    return "mixed"
