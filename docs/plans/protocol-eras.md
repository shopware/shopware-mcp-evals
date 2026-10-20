# Plan: test every MCP protocol revision, era and result format

Status: in progress since 2026-10-20. Phases 0, 1 and 2 are built and pass on the
local trunk lane at shopware 7a96f6086ec (2026-10-20). Phases 3 to 6 are open.

## Why

The suite opens every session with `protocolVersion: 2024-11-05` (`mcp_client.py`,
`mcp_init`). That was a deliberate choice for the session era, but it now hides
most of what core shipped in October:

- shopware/shopware#20948 (result object): `structuredContent` and the
  `resource_link` block are only rendered for 2025-06-18 and later
  (`McpToolResultRenderer::structuredContent()` / `links()`). We never receive
  them.
- shopware/shopware#20951 (signed result pointers): the offload, the principal
  binding and the new Store tool-result resource are not exercised at all.
- Under the `v6.8.0.0` feature flag the first text block loses the
  `{"success", "data", "_meta"}` envelope. About twenty parse sites in this repo
  read exactly those keys, so the suite goes blind the day 6.8 flips.
- The stateless 2026-07-28 era is switched off in core
  (`McpServerBuilderCompilerPass::pinHandshakeEra()`), but the ADR
  `adr/2026-09-24-mcp-stateless-era-degradation-policy.md` says it is switched on
  only "once … covered by tests that run the same scenarios against both eras".
  This repo is the natural place for those scenarios.

## Three axes, not one

| Axis | Values | What changes | How to select it |
|---|---|---|---|
| **Protocol revision** (handshake era) | 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 | Below 2025-06-18: text only, pointer as `_meta.resourceUri` in the text. From 2025-06-18: `structuredContent` (wrapped as `{"result": …}` when not an object), `resource_link` block | `protocolVersion` in `initialize` |
| **Era** | handshake, stateless (2026-07-28) | No `initialize`, no `Mcp-Session-Id`, `server/discover`, per-request `_meta`, `DELETE` → 405, `toolset-enable` errors and points to `?toolsets=`, notifications via `subscriptions/listen` | Client transport; server must stop calling `withoutModernEra()` (shopware/shopware#19969) |
| **Result format** | legacy envelope (6.7), spec-only (`v6.8.0.0`) | First text block holds plain data or the error message; summary, details and `_meta` become separate blocks and move into result `_meta` with a `shopware/` prefix | Feature flag on the lane (`V6_8_0_0=1`) |

The revisions only differ at one boundary today (2025-06-18). Testing every
revision on every run buys nothing, so the matrix is **oldest + boundary +
newest**: 2024-11-05, 2025-06-18, 2025-11-25. The SDK in use (`mcp/sdk` ^0.8.1)
knows all five revisions, so 2025-03-26 is cheap to add if a difference appears.

## Phase 0: one result reader, version on the endpoint (done)

Everything else depends on this, and it changes no verdict on its own.

1. **A single tool-result reader.** Add a `ToolOutcome` (data, is_error, error
   code, error message, error details, meta, links, raw text) built by one
   function that understands all three shapes: legacy envelope in text,
   spec-only text blocks, and `structuredContent`. Prefer `structuredContent`
   when present, fall back to the text.
   Migrate the parse sites to it: `mcp_client.py`, `lane.py`, `ucp.py`,
   `eval/runner.py`, `eval/assertions.py` (`inband_error`), `functional/runner.py`,
   `functional/journeys.py`, `functional/principals.py`. Grep for
   `.get("success")`, `.get("_meta")` and `json.loads(mcp_result_text(` to find
   them all.
2. **Protocol version as an endpoint property.** Like `toolsets`: a
   `protocol_version` field on `Endpoint` and a `with_protocol()` clone that keeps
   the credentials (the Store cart token matters here as it does for
   `with_toolsets()`). `mcp_init` sends it and **fails when the server negotiates
   a different version**, because a silent downgrade would make the whole matrix
   meaningless.
3. **Record it.** Every report row gets `protocol_version` (and later `era`,
   `result_format`). That is an on-disk schema addition: bump `SCHEMA_VERSION`,
   read with `.get()` and a default of `2024-11-05` so older baselines compare.
4. **Unit tests** under `tests/` with fake responses for each shape: legacy,
   spec-only, structured-object, structured-wrapped list, error with code,
   error without code (merchant and UCP tools that predate the code).

Default stays 2024-11-05 at the end of this phase, so CI numbers must not move.

## Phase 1: functional suite across revisions (done)

As built: `functional/result_format.py` opens a session per revision itself, so
there is no `--protocol` list and no CI loop. Every run of `functional.runner`
covers the matrix, admin and store. A server older than #20948 records one skip
naming the PR instead of failing every probe. The missing-privilege check is part of
the allowlist matrix. The one-tool user calls its allowed tool on `product`, which its
role does not grant, and must get `missing_privilege`.

Free, no model, so it can run on every push of the eval workflow.

1. `functional.runner --protocol 2024-11-05,2025-06-18,2025-11-25` runs the
   result-format checks once per revision. The discovery and per-tool checks run
   once, under the newest revision, to keep the job short.
2. New checks, all on known inputs:
   - **Failure shape.** `shopware-entity-schema` with `no_such_entity`:
     `isError: true` on every revision, `structuredContent.error.code ==
     "invalid_arguments"` from 2025-06-18, no `structuredContent` before.
   - **Not found.** `shopware-entity-read` on a phantom id: code `not_found`.
   - **Missing privilege.** Reuse a principal from `functional/principals.py`
     (behind `--provision-principals`): a refused call carries
     `missing_privilege`.
   - **Success shape.** `shopware-toolsets-list`: object `structuredContent`; a
     tool returning a list gets `{"result": [...]}`.
   - **Metadata.** `_meta["shopware/generatedAt"]` present on the result.
   - **Text and structured agree.** The text copy and `structuredContent` carry
     the same data, as the spec asks.
3. Same checks on the Store endpoint. UCP tools return envelope strings that the
   core parser converts, so `dryRun` and `preview` must arrive as metadata.
4. CI: the `static` job loops over the three revisions for admin and store.
   Each loop writes its own report section; a revision-specific failure names the
   revision in its check label.

## Phase 2: large results and signed pointers (#20951) (built)

As built, in `functional/large_results.py`:

- The reproducer needs no seeding. On the local lane's demo data it returned
  346 KB, measured on 2026-10-20.
- The offload is checked on 2024-11-05 and 2025-06-18. The binding checks are: the
  same principal on a new session, a tampered expiry under the same signature, and
  a second integration (behind `--provision-principals`).
- The Store half is a recorded skip. No Store tool goes through core's offloading
  response: the UCP tools render their own results, and
  `shopware-store-api-context` stays far below 100 KB. The context-token checks
  wait for a Store tool that can return a large result.
- The legacy plain-id fallback is not checked. A server older than #20951 is
  recorded as one skip naming the PR.
- Eval side (item 5): an answer stored behind a pointer fails a `data` fixture as
  `offloaded`, is recorded as `offloaded` on the result, and is named in a warning at
  the end of the run. The `accepted` tier still passes, because the call ran.
- Passes live on the local trunk lane. On 2025-06-18 the reply carries a
  `resource_link` block with a signed URI, and on 2024-11-05 it does not.


1. **Trigger an offload.** The PR's own reproducer
   (`shopware-entity-search` on `country` with `states`, limit 500). Verify on
   the lane that it really exceeds 100 KB; if demo data is too small, pick a
   larger association rather than seeding.
2. **Shape per revision.** Before 2025-06-18: signed `_meta.resourceUri` and
   `_meta.expiresAt` in the text, no `resource_link`. From 2025-06-18: a
   `resource_link` block with the same URI and `_meta["shopware/expiresAt"]`.
3. **Who may read it.**
   - Same credentials, **new session**: `resources/read` returns the full result.
   - Another principal (a throwaway integration from `principals.py`): not found.
   - Store, same `sw-context-token`: readable. Different token: not found.
   - Store, no context token: the result stays inline, no pointer.
4. **Not tested here:** the one-hour expiry and the legacy plain-id fallback.
   Both are time-bound and covered by core unit tests; waiting an hour in CI is
   not worth it. Record them as skips with that reason, not silent omissions.
5. **Eval impact.** The eval gives the model no way to call `resources/read`, so
   an offloaded result is a preview the model cannot expand. Add a check that
   fails the eval loudly if any fixture's result was offloaded, rather than
   quietly grading a truncated answer. Exposing `resources/read` to the model is
   a separate decision and out of scope.

## Phase 3: grade on error codes

1. `inband_error` and `mcp_call_error` return the code next to the message.
2. `assertions.check` decides `not_found` and `invalid_arguments` from the code
   first, and falls back to the current prose and UCP-prefix matching for tools
   that send no code (merchant tools, agentic-commerce).
3. Count how often the fallback fires per owner (`ownership.py`). That number is
   the list of plugins still to migrate to coded errors, and it should only fall.

## Phase 4: next-major lane (`v6.8.0.0`)

1. `setup-lane` gets a `feature_flags` input written to `.env.local` and to the
   daemonised server's environment. The "plain http" UCP row in AGENTS.md is the
   warning: the CLI, the server and the runner are three environments.
2. A new **advisory** nightly job, `next-major`, builds a lane with
   `V6_8_0_0=1` and runs the functional suite for admin and store under the
   newest revision. After Phase 0 the reader handles the spec-only format, so a
   failure here is a real server or plugin problem, attributed to its owner.
3. Opt-in on PRs with a `run-next-major` label, like `run-preloaded`.
4. Expect plugins to break under the major flag before core does. That is the
   point of the job, and why it stays advisory until the plugins are clean.

## Phase 5: stateless era (2026-07-28)

Core does not serve it yet, so this starts as a probe and grows into the ADR's
scenario list once shopware/shopware#19969 lands.

1. **Client transport.** A stateless request path in `mcp_client.py`: no
   `initialize`, no session header, protocol version and client capabilities in
   each request's `_meta`, `server/discover` for capabilities. Build it against
   the 2026-07-28 spec text and the SDK's own client, not from the ADR summary.
2. **Probe.** On every run, try `server/discover`. While core still pins the
   handshake era, record a skip naming `withoutModernEra()` and #19969, the same
   pattern as `PINNED_REPORTED_DISABLED`. When the probe starts answering, the
   skip turns into failures until the scenarios below exist, so nobody forgets.
3. **Scenarios from the ADR, both eras:**
   - No `Mcp-Session-Id` is minted.
   - `?toolsets=` pins visibility identically on both eras.
   - `shopware-toolset-enable` returns `isError` and points to `?toolsets=`, or
     is not offered at all.
   - A large result is offloaded with a signed pointer, readable by the next
     request from the same principal.
   - `DELETE` answers 405.
   - `tools/list_changed` arrives through `subscriptions/listen` after an app
     change. Last, because it needs a stream and holds a worker.
4. **Eval arm `--era stateless`.** Advisory and opt-in. The model connects with
   `?toolsets=` and cannot enable anything, so it measures what claude.ai-style
   clients get. Compare it with the `preloaded` arm, which is the closest
   handshake equivalent.

## Phase 6: move the eval to the revision real clients use

1. Find out which revision Claude, ChatGPT and the MCP Inspector negotiate today,
   and **which content they hand the model**: the text block, `structuredContent`,
   or both. This is the open question that decides what the eval should feed.
2. Run one paired eval: 2024-11-05 against the newest revision, same commit.
   Under the 6.7 format the model-facing text is identical, so pass rates should
   match within noise. If they do, switch the default. If they do not, find out
   what moved before touching the gate.
3. Record `structuredContent` bytes next to `payload_bytes`. A client that passes
   both pays for the data twice, and `cost_drift` should see that.

## Order and cost

| Phase | Depends on | CI cost | Gating |
|---|---|---|---|
| 0 reader + version | nothing | none | unit tests only |
| 1 revision matrix | 0 | small, functional only | gating, it is deterministic |
| 2 large results | 0, 1 | small | gating |
| 3 error codes | 0 | none | changes grading; verify on one run first |
| 4 next-major lane | 0 | one extra lane, nightly | advisory |
| 5 stateless era | 0, core #19969 | small until scenarios exist | probe skip, then gating |
| 6 eval default | 0, 1, open question answered | one paired eval run | unchanged gates |

Phases 0 to 3 can ship in two or three PRs within the current workflow. Phase 4
adds a job. Phase 5 waits on core and should be built together with whoever
lands #19969, because its scenarios are that PR's acceptance test.

## Open questions

- Which content blocks do real clients pass to the model, and does any of them
  read `structuredContent` instead of text?
- The exact 2026-07-28 wire format for a request without a session: verify in
  the spec and in `mcp/sdk` before writing the transport.
- `composer.json` allows `mcp/sdk` 0.9.0. Check whether it changes revision
  handling before Phase 5.
- Does the demo data on the lane produce a result above 100 KB, or does Phase 2
  need a seeded fixture?
