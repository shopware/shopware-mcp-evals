# What the suite covers

One row per test case: what it proves, which axes it varies, how it runs, and
why each combination it leaves out is left out. The plan behind this document
is https://github.com/shopware/shopware-mcp-evals/pull/104.

This page covers the functional layer and what is checked by hand. The LLM eval
runs on the admin and Store endpoints at 2025-11-25, as the administrator user
key, on the default lane, and is described in `AGENTS.md`.

## How to read a row

The **how** column starts with one of three words.

- **auto** names the CI jobs in backticks and at least one fragment of the
  check's label in double quotes. Most labels are built at run time, so a
  fragment is the literal part the source contains.
- **manual** says what a person does, and where the steps are.
- **none** means nothing covers it. The last column says why, or what it would
  take.

`tests/test_coverage_doc.py` fails when an `auto` row names a job the workflow
does not have, or a label fragment that no longer appears in `functional/`.
Rename a check and this page has to follow.

## The axes

| axis | values |
|---|---|
| principal | administrator user key (`SWUA…`), integration (`SWIA…`), non-admin user key, OAuth user token |
| revision | 2024-11-05, 2025-06-18, 2025-11-25 |
| era | handshake, stateless 2026-07-28 |
| result format | 6.7 envelope, 6.8 spec format |
| endpoint, session store | admin or Store; file or Redis |

The jobs that run the functional suite, and where they sit on those axes:

| job | revision | result format | session store | principals | Store suite |
|---|---|---|---|---|---|
| `static` | 2025-11-25 | 6.7 | file | admin key, plus the allowlist matrix | yes, advisory |
| `revisions` | 2024-11-05 and 2025-06-18, one job each | 6.7 | file | admin key, plus the allowlist matrix | yes, advisory |
| `next-major` | 2025-11-25 | 6.8 (`V6_8_0_0`) | file | admin key, plus the allowlist matrix | yes, advisory |
| `session-store` | 2025-11-25 | 6.7 | Redis | admin key only | no |

Unless a row says otherwise, the principal is the administrator user key.

## Discovery

| test case | axes | how | why not |
|---|---|---|---|
| A fresh session advertises the meta-tools and no deferred tool. | endpoint: admin, Store. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "no deferred tools leak into the default surface", "advertised by default" | Store on Redis: `session-store` runs the admin suite only. |
| `?toolsets=` pins one toolset, several, or all before the first `tools/list`, ignores an unknown name, and leaves a plain connect alone. | endpoint: admin, Store. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "advertises exactly that toolset", "advertises the union", "ignores the unknown name", "a plain connect is unaffected by the feature" | Store on Redis: as above. |
| toolsets-list reports the pinned toolsets as enabled. | endpoint: admin, Store. revision: all three. | auto: `static`, `revisions`, `next-major`, `session-store`. "toolsets-list reports pinned toolsets as enabled" | Recorded as a skip naming https://github.com/shopware/shopware/pull/21772 while `shopware.sha` predates it. |
| toolsets-list returns well-formed toolsets that cover the deferred tools. | endpoint: admin, Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "every toolset has name/title/description/tools and enabled=false", "toolsets cover", "toolsets defer" | |
| toolset-enable grows `tools/list` and answers `listChanged`. | endpoint: admin, Store. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "succeeds with _meta.listChanged=true", "advertised after enabling" | |
| The default tools stay advertised after an enable. | endpoint: admin. revision: all three. | auto: `static`, `revisions`, `next-major`, `session-store`. "default tools still advertised after enable" | Store: not checked. The Store run turns the check off and no reason is recorded, so this is a gap. |
| Enablement does not leak into another session. | endpoint: admin, Store. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "toolset enablement does not leak across sessions" | |
| tool-search finds a deferred tool and caps its results at 20. | endpoint: admin. revision: all three. | auto: `static`, `revisions`, `next-major`, `session-store`. "tool-search finds deferred shopware-media-upload", "tool-search caps maxResults at 20" | Store: its tools are few, so it checks ranking instead, in the next row. |
| tool-search ranks a UCP cart tool in its first five results for a cart query. | endpoint: Store. revision: all three. | auto: `static`, `revisions`, `next-major`. "ranks a UCP cart tool for a cart query" | |
| Enabling every toolset in one session reaches the whole catalogue. | endpoint: admin. revision: all three. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "activated all", "all tools reachable" | Store: the journey enables every toolset and then calls the tools, which proves more. |
| Every advertised tool has an object-typed `inputSchema`, before an enable, after one, and in tool-search results. | endpoint: admin. revision: all three. | auto: `static`, `revisions`, `next-major`, `session-store`. "expose an object-typed inputSchema.properties", "tool schema conformance" | Store: not run, and no reason is recorded, so this is a gap. |
| `shopware-store-api-context` is deferred but directly callable, and returns a channel and a token. | endpoint: Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "shopware-store-api-context (deferred, callable, returns channel+token)" | |

## Tool calls

| test case | axes | how | why not |
|---|---|---|---|
| Every admin tool in the check table answers a minimal call, dry-run where it mutates, and the result is asserted. | endpoint: admin. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "shopware-entity-search", "shopware-media-upload" | Other principals: the allowlist matrix calls one probe tool per principal, not the table. |

## Who calls: the allowlist matrix

The principals are created for the run and deleted after it, behind
`--provision-principals`. Each is checked on `tools/list`, `?toolsets=all`,
toolsets-list, toolset-enable, tool-search, `resources/list`, `prompts/list` and
a direct call.

| test case | axes | how | why not |
|---|---|---|---|
| The administrator user key is unrestricted and reaches the whole catalogue. | principal: admin user key. revision: all three. format: both. store: both. | auto: `static`, `revisions`, `next-major`, `session-store`. "all tools reachable" | |
| An integration with no allowlist sees the meta-tools only, and every call is refused. | principal: integration, flagged admin. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "integration-unset", "is listed no toolsets", "reaches only the meta-tools with ?toolsets=all" | Redis: the allowlist does not depend on where sessions live. |
| An integration with "All capabilities" reaches exactly what the administrator reaches. | principal: integration, flagged admin. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "integration-all", "is listed every toolset", "is shown every tool-search hit" | Redis: as above. |
| An integration allowlisted one tool reaches that tool, and a tool from another toolset is refused. | principal: integration, flagged admin. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "integration-one-tool", "is shown only reachable tools by tool-search" | Redis: as above. |
| A non-admin user with no allowlist sees the meta-tools only, and every call is refused. | principal: non-admin user key. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "user-unset" | Redis: as above. |
| A non-admin user allowlisted one tool reaches it, and its ACL role still refuses it on `product` with `missing_privilege`. | principal: non-admin user key. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "user-one-tool", "on product with missing_privilege" | Redis: as above. |
| A non-admin user with "All capabilities" reaches the saved selection and nothing added after it. | principal: non-admin user key. | none | Gap 1 in the plan. It is the setting a shop gives a real person. |
| An administrator user with a saved allowlist, a subset or empty, is still unrestricted. | principal: admin user key. | none | Gap 2 in the plan. The bypass is the documented rule and nothing holds the server to it. |
| An integration not flagged admin, allowlisted a tool its role does not grant, gets `missing_privilege`. | principal: integration, not flagged admin. | none | Gap 3 in the plan. Every integration in the matrix is flagged admin, so ACL never applies to one. |

The OAuth principals join this table with
https://github.com/shopware/shopware-mcp-evals/pull/103.

## Across revisions

These checks open their own session on each revision, whatever the run
negotiated, so only the 2025-11-25 run carries them. The `revisions` jobs record
a skip saying so: "result format, large results, stateless era".

| test case | axes | how | why not |
|---|---|---|---|
| A result has the right shape on each revision: `structuredContent` exactly from 2025-06-18 and agreeing with the text, `isError`, the error code and the `generatedAt` stamp. | endpoint: admin, Store. revision: all three, in one run. format: both. | auto: `static`, `next-major`, `session-store`. "result format", "unknown entity", "missing record" | A server older than https://github.com/shopware/shopware/pull/20948 records one skip naming it. |
| A result above 100 KB is stored behind a signed pointer, in the shape each revision expects. | endpoint: admin. revision: 2024-11-05 and 2025-06-18, in one run. format: both. store: both. | auto: `static`, `next-major`, `session-store`. "stored behind a signed pointer" | 2025-11-25: the pointer's shape changes at 2025-06-18 and nowhere else, so one revision on each side is enough. Store: no Store tool can be made to offload, and "large results (store)" records that. |
| The pointer reads back for the same principal on a new session, and a later expiry under the same signature is refused. | endpoint: admin. revision: the run's own. store: both. | auto: `static`, `next-major`, `session-store`. "the same principal reads it on a new session", "a later expiry under the same signature is refused" | The one-hour expiry is time-bound and covered by core's unit tests: "expires after an hour" is a recorded skip. |
| Another principal is refused the pointer. | principal: a second integration. endpoint: admin. | auto: `static`, `next-major`. "another principal is refused" | `session-store` runs without `--provision-principals` and records a skip. |
| The stateless 2026-07-28 revision is refused, and the refusal lists the handshake revisions. | era: stateless. endpoint: admin, Store. | auto: `static`, `next-major`, `session-store`. "handshake revisions offered", "stateless " | A skip until https://github.com/shopware/shopware/issues/19969. An answer fails until the stateless scenarios exist. |

## Session store

Run by `python -m functional.sessions` on the Redis lane, against both endpoints
when the lane has a sales-channel key.

| test case | axes | how | why not |
|---|---|---|---|
| `initialize` writes the session to Redis, and the key expires with the session's TTL. | endpoint: admin, Store. store: Redis. | auto: `session-store`. "session written to Redis", "session key expires with the session TTL" | File store: nothing to measure. |
| A session is refused on the other endpoint. | endpoint: admin, Store. store: Redis. | auto: `session-store`. "session refused on " | Without a sales-channel key there is no Store session to compare, and it records a skip. |
| `DELETE` removes the session from Redis. | endpoint: admin, Store. store: Redis. | auto: `session-store`. "DELETE removes the session from Redis" | |
| The server refuses a session once its Redis key is gone, with 404 and `Session not found or has expired.` | endpoint: admin, Store. store: Redis. | auto: `session-store`. "session refused once its Redis key is gone" | |

## UCP buyer journey

The journey commits: it creates a cart and a checkout and places a real order.
It runs behind `--allow-mutations`, which CI passes because the lane dies with
the job. The Store suite is advisory in every job.

| test case | axes | how | why not |
|---|---|---|---|
| A guest finds a product, fills and edits a cart, applies a promotion code, checks out and places an order, and cancels a cart. | endpoint: Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "find a product to buy", "apply a promotion code", "place the order", "cancel the cart" | Redis: `session-store` runs no Store suite. |
| A guest is refused its own order, because the sales-channel key is shared. | endpoint: Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "read the placed order back" | Redis: as above. |
| A signed-in customer runs the same journey and reads the placed order back. | endpoint: Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "customer: read the placed order back" | Redis: as above. |
| A signed-in customer can place a second order. | endpoint: Store. revision: all three. format: both. | auto: `static`, `revisions`, `next-major`. "a signed-in buyer can place a second order" | A known failure, https://github.com/shopware/agentic-commerce/issues/163. |

## By hand, or not at all

| test case | axes | how | why not |
|---|---|---|---|
| claude.ai connects through the real consent page, with `?toolsets=` on the URL. | client: claude.ai. principal: OAuth user. | manual: add the lane as a custom connector, approve on the consent page, and list the tools. | A browser and a claude.ai account; no CI runner has either. |
| Claude Code connects through the real consent page. | client: Claude Code. principal: OAuth user. | manual: add the lane on `localhost:8100` with `claude mcp add`, approve, and list the tools. | A browser. The flow itself is automated with a probe client in https://github.com/shopware/shopware-mcp-evals/pull/103. |
| The Administration's "All capabilities" switch saves a snapshot, so a tool added later is not granted and the switch reads as off. | principal: integration. | manual: save "All", add a tool, and reopen the dialog. | The switch is Administration UI. The suite builds the same snapshot through `/api/_action/mcp/capabilities`. |
| `?toolsets=` survives into the OAuth resource metadata, and the token works on both URLs. | principal: OAuth user. | manual: round 3 in the plan. | Becomes a check in the OAuth module once https://github.com/shopware/shopware-mcp-evals/pull/103 merges. |
| A plain-http redirect URI outside loopback is refused, and https and `cursor://` go through. | client: Cursor, any https client. | manual: round 3 in the plan. | Becomes a check once setup-lane registers a third client with those redirect URIs. |
| Which protocol revision each real client negotiates, and what it passes to the model. | client: Claude Code, MCP Inspector, claude.ai, Cursor. revision, era. | manual: put a logging proxy in front of the lane and read each client's first requests. Claude Code and MCP Inspector were measured on 2026-10-22, and the results are in the plan. | claude.ai and Cursor are still open. They need a person, and the answer decides which client combinations need automation. |
| A ChatGPT connector. | client: ChatGPT. | none | Nobody has connected one yet. |
| Only MCP-bound tokens are accepted on subrequests the MCP endpoint marks. | principal: OAuth user. | none | It matters for app script tools only, and the lane has none. |
