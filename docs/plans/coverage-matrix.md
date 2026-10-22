# Plan: one coverage matrix, and a summary that renders it

Status: planned 2026-10-22, nothing built. Waits for
https://github.com/shopware/shopware-mcp-evals/pull/103 (OAuth), which changes
the reporter and the principals code both parts read.

## Why

The suite now varies five things, and nothing says which test case runs on
which combination, which combinations are skipped on purpose, and what is only
ever checked by hand.

- The job summary shows the LLM eval and the tool catalogue only. The
  functional suite, which is the layer that runs per revision, per result format
  and per principal, has no row. Its results exist as annotations.
- `report` does not wait for the protocol jobs or next-major, so their reports
  are collected only when those jobs happen to finish first, and nothing reads
  them even then.
- A functional report records its protocol revision but not its lane, so
  next-major and the default run, both at 2025-11-25, look the same.
- OAuth adds a principal, and the OAuth round below found three things the
  suite does not cover. They were probed by hand and are recorded nowhere else.

## The axes

| Axis | Values | Today |
|---|---|---|
| **Principal** | admin user key (`SWUA…`), integration (`SWIA…`), non-admin user key, OAuth user token | The whole suite runs as the admin user key. Integrations and non-admin users run the allowlist surfaces (`--provision-principals`). OAuth runs its flow plus initialize, `tools/list` and `tools/call` (https://github.com/shopware/shopware-mcp-evals/pull/103) |
| **Protocol revision** (client side) | 2024-11-05, 2025-06-18, 2025-11-25 | Functional suite on each; LLM eval on 2025-11-25 |
| **Era** (client side) | handshake, stateless 2026-07-28 | Stateless is one skip until https://github.com/shopware/shopware/issues/19969 |
| **Result format** | 6.7 envelope, 6.8 spec format | 6.8 on next-major only |
| **Endpoint, session store** | admin or store; file or Redis | Both endpoints; Redis on `session-store` |

Most axes are independent, so the matrix is not a cross-product. Each test case
names the axes it varies, and every combination left out gets a reason. For
example, OAuth token binding does not depend on the protocol revision, so OAuth
runs at the default revision and the matrix jobs record a skip.

## Who calls: the principal

"Who calls" is three axes of its own: the kind of principal, the credential,
and the MCP allowlist it holds. The allowlist is the gate for everyone except an
administrator user (https://github.com/shopware/shopware/pull/20600), and ACL applies on top of it for
anyone not flagged admin.

Today, with https://github.com/shopware/shopware-mcp-evals/pull/103 merged:

| principal | credential | allowlist unset | "All capabilities" (full selection) | a subset |
|---|---|---|---|---|
| integration, flagged admin | access key `SWIA…` | auto: blocked, meta-tools only | auto: reaches what the admin user reaches | auto: one tool, another toolset refused |
| integration, not flagged admin | access key `SWIA…` | none | none | none |
| non-admin user | access key `SWUA…` | auto: blocked | **none** | auto: one tool, plus `missing_privilege` from its ACL role |
| non-admin user | OAuth token | auto: meta-tools only | **none** | auto: three tools, another refused |
| admin user | access key `SWUA…` | auto: the whole suite, unrestricted | **none**: must still be unrestricted | **none**: must still be unrestricted |
| admin user | OAuth token | auto: the OAuth flow, unrestricted | **none** | **none** |

The access-key principals are checked on every surface: `tools/list`,
`?toolsets=all`, toolsets-list, toolset-enable, tool-search, `resources/list`,
`prompts/list` and a direct call. The OAuth users are checked on `tools/list`
and calls only.

Gaps to close, in the order they would surprise someone:

1. **A non-admin user with "All capabilities"**, on a key and on an OAuth
   token. It is the setting a shop gives a real person, and the one state no
   check holds a user to. It also needs the snapshot rule stated: a tool added
   after the save is not granted.
2. **An admin user with an allowlist saved.** The bypass is the documented
   rule, and nothing checks that a saved allowlist is ignored for an admin,
   neither for a subset nor for an empty list.
3. **An integration not flagged admin.** Every integration in the matrix is
   flagged admin, so ACL never interferes. One without the flag, allowlisted a
   tool its role does not grant, must answer `missing_privilege`, as the
   non-admin user does.
4. **OAuth users on the other surfaces.** The same eight surfaces the
   access-key principals run, so a token cannot reach through tool-search or
   `?toolsets=all` what its allowlist withholds.
5. **ACL under OAuth.** A non-admin OAuth user allowlisted a tool its role does
   not grant must get `missing_privilege`, as on a key.

Not applicable, with the reason, so the matrix does not read as gaps:

- **An integration with an OAuth token.** The code grant signs in a person.
  Whether an integration's `client_credentials` token is accepted on
  `/api/_mcp` is a question for core first. If it is, it becomes a row.
- **The Store endpoint.** It authenticates with a sales-channel key and has no
  principals or allowlist.

Each principal row runs at the default revision only. The allowlist is decided
before the result is rendered, so a revision cannot change who reaches a tool,
and the matrix jobs record a skip saying so.

## Principal and client are two matrices

The table above is the **principal**: who the user is and what the shop grants
them. That is server-side configuration. The **client** is how they connect,
and the client chooses the rest: the credential flow, the protocol revision, the
era, and whether it pins toolsets or enables them. So the revisions and eras are
columns of the client matrix, not of the principal one.

The client axes:

| axis | values |
|---|---|
| credential flow | access key; OAuth with a loopback redirect, an https redirect, or a private-use scheme (`cursor://`) |
| revision and era | handshake at 2024-11-05, 2025-06-18 or 2025-11-25; stateless 2026-07-28 |
| tool loading | reads `tools/list` once and needs `?toolsets=` (claude.ai); or enables toolsets during the session |
| what it hands the model | the text block, `structuredContent`, or both (protocol-eras Phase 6 item 1, open) |

Real clients as rows, so a gap reads as "Cursor is not tested" rather than as a
missing axis value:

| client | credential | redirect | revision | tool loading | how tested |
|---|---|---|---|---|---|
| a script with access keys (this suite) | key | none | all three handshake revisions | enables toolsets | auto: every functional job |
| Claude Code | OAuth, or headers | loopback, any port on `127.0.0.1` | tries stateless 2026-07-28 first, falls back to 2025-11-25 | enables toolsets; follows `list_changed` | auto: the flow with the probe client; manual: the real client against `localhost:8100` |
| claude.ai | OAuth | https | unknown | reads once, needs `?toolsets=` | manual. `?toolsets=` with OAuth was probed by hand (round 3) and becomes a check |
| Cursor | OAuth | `cursor://` | unknown | unknown | manual: the redirect was probed by hand; becomes a check with the third client |
| ChatGPT connector | OAuth | https | unknown | unknown | none |
| MCP Inspector | either | loopback | 2025-11-25 | enables toolsets | none |

Every "unknown" is the same open question as protocol-eras Phase 6 item 1:
which revision each client negotiates and what it passes to the model. Finding
out fills the column and decides which combinations need automation.

### Measured: Claude Code and MCP Inspector

Measured 2026-10-22 on the trunk lane at `71b350a48e6`, through a logging proxy
in front of `localhost:8100`, authenticated with the admin user key in headers.

| | Claude Code 2.1.308 | MCP Inspector CLI 2.12.0 |
|---|---|---|
| first request | `server/discover` at 2026-07-28 | `initialize` |
| after the refusal | `initialize` at 2025-11-25 | (none) |
| negotiated | 2025-11-25 | 2025-11-25 |
| hands the model | `structuredContent`, as compact JSON | no model; prints the whole result |
| after toolset-enable | re-reads `tools/list` on `list_changed` | not tested |

What follows from it:

- **Claude Code moves to the stateless era the day core serves it.** It probes
  `server/discover` before every `initialize`, and today's refusal (-32022,
  listing the four handshake revisions) is what keeps it on the handshake. So
  the stateless column is not hypothetical: once
  https://github.com/shopware/shopware/issues/19969 lands, every Claude Code
  user is on it with no client change.
- **Claude Code's model never sees the text blocks.** The session transcript
  records the tool result it handed the model, and it equals the call's
  `structuredContent` byte for byte. On this lane that is the data with `_meta`
  merged in; the text is two blocks, the data and a separate `_meta` block. The
  eval feeds the text blocks, so it grades a different payload than Claude Code
  sends.
- **`list_changed` arrives late but works.** The toolset-enable answer was plain
  JSON. The notification came on the event stream of the next call, and Claude
  Code then re-read `tools/list` and found the enabled tools.
- **Claude Code defers MCP tools behind its own tool search.** Every server tool
  is loaded through Claude Code's ToolSearch before its first call, so its model
  meets the server's tools through two layers of discovery.

Not yet measured: claude.ai and Cursor, which need a person, and the ChatGPT
connector. The `{"success", "data", "_meta"}` envelope was not seen either: at
this commit, with `V6_8_0_0` off, the lane already sent the text as plain data
followed by a `_meta` block. Why it does is not yet understood, and it decides
whether the eval's text and Claude Code's `structuredContent` differ by more than
where `_meta` sits.

The era column is mostly empty for now. Every client above uses the handshake
era, and stateless is switched off in core until
https://github.com/shopware/shopware/issues/19969. When it opens, it adds one
column to the client matrix: the same rows over `server/discover`, without a
session. Three cells then change meaning:

- **toolset-enable** answers an error and points to `?toolsets=`, so "enables
  toolsets" stops being a way to load tools.
- **OAuth** carries the token on every request, with no session to bind it to.
- **Session store** stops mattering for that column, so the Redis lane does not
  need a stateless run.

The summary's lane table carries the client side (revision and era) and the
shop's own settings (result format, session store). Its "who calls" split
carries the principal side.

## Part 1: `docs/coverage.md`

One row per test case:

| column | content |
|---|---|
| test case | what it proves, one sentence |
| axes | which of the five it varies, and on which values |
| how | `auto`: the check label and the job; `manual`: the steps; `none`: not covered |
| why not | for each combination left out, one line |

Drift guard: every `auto` row names a check label, and a unit test fails when a
named label no longer appears in `functional/`, the way
`tests/test_known_failures.py` holds the registry to the suite.

Manual rows to start with:

- claude.ai and Claude Code connecting through the real consent page in a browser.
- The Administration's "All capabilities" switch, and its snapshot behaviour.
- A ChatGPT connector.
- The OAuth probes below until they become checks.

## Part 2: the summary

1. **A functional table**, one row per lane (default, 2024-11-05, 2025-06-18,
   6.8, Redis), split by who calls (admin key suite, allowlist principals,
   OAuth): passed, new failures, expected failures (with the issue), skipped.
2. **Differs by lane**: checks whose result changes between lanes. One line
   when every lane agrees.
3. **`lane` and `principal` on each functional record and report**, added
   fields under the report-schema rule in AGENTS.md, so the summary does not
   guess from labels.
4. **`report` needs the protocol jobs.** They are required checks anyway.
   next-major stays out of `needs`, as AGENTS.md asks, and its row reads "not
   finished" instead of disappearing.
5. **One line naming the LLM eval's revision.**

## OAuth: round 3 and what it left open

Run by hand on the trunk lane, core detached at `71b350a48e6`
(`integration/mcp-iteration-4`, 20 commits past round 2's `c39f200ba50`), with
the suite of https://github.com/shopware/shopware-mcp-evals/pull/103:

| revision | passed | failed | skipped |
|---|---:|---:|---:|
| 2025-11-25, the default | 182 | 0 | 4 |
| 2025-06-18 | 101 | 0 | 3 |
| 2024-11-05 | 101 | 0 | 3 |

All 50 OAuth checks pass, including the non-admin users under
`--provision-principals`. The skips are the one-hour expiry, the stateless
revision, the two dev-tools log tools, and the per-revision skip on the matrix
runs. No test users, roles or integrations were left on the lane. The new
commits needed no rebuild: the only Administration change was two consent-page
snippets, with no composer or migration changes.

Probed by hand, not yet in the suite:

- **The `?toolsets=` query is kept in the resource metadata.** For
  `/api/_mcp?toolsets=all` and `?toolsets=order,media`, the challenge's
  `resource_metadata` URL and the metadata `resource` both keep the query. A
  grant with that query in the resource still gets a token whose audience is
  the bare endpoint, and the token works on both URLs. *Becomes a check in
  `functional/oauth.py`.*
- **Plain-http redirect URIs outside loopback are refused.** On the prod-mode
  lane, with a temporary client and one authorize per URI: an http
  non-loopback URI is answered 400 naming `FRAMEWORK__OAUTH_INSECURE_REDIRECT_URI`,
  and nothing is sent to it. An https URI and a `cursor://` private-use URI both
  go through consent and get a code. *Becomes a check once setup-lane registers
  a third client with those redirect URIs.* The probe scripts are in
  `/tmp/oauth-e2e`, which does not survive a reboot.

Not tested at all:

- **Only MCP-bound tokens are accepted on subrequests the MCP endpoint marks.**
  It matters for app script tools only, and the lane has none. Needs an app
  with a script tool on the lane, or stays a `none` row with that reason.

The "How to check" section of https://github.com/shopware/shopware-mcp-evals/pull/103 still names `c39f200ba50`; the numbers
are the same at `71b350a48e6`.

## Order

1. https://github.com/shopware/shopware-mcp-evals/pull/103 merges.
2. `docs/coverage.md` with its drift test, one PR. Cheap, and it settles the
   matrix the summary renders.
3. The principal gaps above, in `functional/principals.py` and
   `functional/oauth.py`, one PR. Each adds a principal to the throwaway set,
   so the cleanup in `provisioned()` covers it.
4. The two OAuth probes as checks, the third client in setup-lane.
5. The summary rework, one PR. Its "who calls" split follows the principal
   table above.
