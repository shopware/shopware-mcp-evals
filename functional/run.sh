#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Shopware MCP Functional Test Runner (MCP Server v2: dynamic tool discovery)
# Requires: SW_BASE_URL, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY
# Usage: bash functional/run.sh [--skip-media-upload] [--skip-dev-tools]
# ---------------------------------------------------------------------------

SKIP_MEDIA_UPLOAD=false
SKIP_DEV_TOOLS=false
for arg in "$@"; do
  [[ "$arg" == "--skip-media-upload" ]] && SKIP_MEDIA_UPLOAD=true
  [[ "$arg" == "--skip-dev-tools" ]] && SKIP_DEV_TOOLS=true
done

: "${SW_BASE_URL:?SW_BASE_URL is required}"
: "${SW_ACCESS_KEY:?SW_ACCESS_KEY is required}"
: "${SW_SECRET_ACCESS_KEY:?SW_SECRET_ACCESS_KEY is required}"

# Load .env if present
[[ -f "$(dirname "$0")/../.env" ]] && source "$(dirname "$0")/../.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
PASS=0; FAIL=0; SKIP=0
RESULTS_FILE="$(dirname "$0")/../results/functional-$(date +%Y%m%d-%H%M%S).json"
RESULTS='[]'

log_pass() { echo -e "  ${GREEN}PASS${RESET} $1"; }
log_fail() { echo -e "  ${RED}FAIL${RESET} $1: $2"; }
log_skip() { echo -e "  ${YELLOW}SKIP${RESET} $1"; }

# ---------------------------------------------------------------------------
# Record a non-tool check result (structural assertions on lists etc.)
# ---------------------------------------------------------------------------
check_pass() {
  local label=$1
  log_pass "$label"
  PASS=$((PASS+1))
  RESULTS=$(echo "$RESULTS" | CHECK_LABEL="$label" python3 -c "
import json,sys,os
r=json.load(sys.stdin)
r.append({'tool':'check','label':os.environ['CHECK_LABEL'],'status':'pass'})
print(json.dumps(r))
")
}

check_fail() {
  local label=$1 err=$2
  log_fail "$label" "$err"
  FAIL=$((FAIL+1))
  RESULTS=$(echo "$RESULTS" | CHECK_LABEL="$label" CHECK_ERR="$err" python3 -c "
import json,sys,os
r=json.load(sys.stdin)
r.append({'tool':'check','label':os.environ['CHECK_LABEL'],'status':'fail','error':os.environ['CHECK_ERR']})
print(json.dumps(r))
")
}

# ---------------------------------------------------------------------------
# MCP session init — returns session ID via stdout
# ---------------------------------------------------------------------------
mcp_init() {
  local resp session
  resp=$(curl -si -X POST "${SW_BASE_URL}/api/_mcp" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_ACCESS_KEY}" \
    -H "sw-secret-access-key: ${SW_SECRET_ACCESS_KEY}" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp-eval","version":"2.0"}},"id":1}' \
    2>/dev/null)
  session=$(echo "$resp" | grep -i "^Mcp-Session-Id:" | tr -d '\r' | awk '{print $2}')
  echo "$session"
}

# ---------------------------------------------------------------------------
# Call a tool and return the raw JSON response
# ---------------------------------------------------------------------------
mcp_call() {
  local session=$1 tool=$2 args=$3
  curl -s -X POST "${SW_BASE_URL}/api/_mcp" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_ACCESS_KEY}" \
    -H "sw-secret-access-key: ${SW_SECRET_ACCESS_KEY}" \
    -H "Mcp-Session-Id: ${session}" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"${tool}\",\"arguments\":${args}},\"id\":99}" \
    2>/dev/null
}

# ---------------------------------------------------------------------------
# Fetch the first entity's field value (default: id) via entity-search.
# Retries a few times — a large product payload piped through curl can
# occasionally deliver a partial read. Echoes '' on failure so callers can
# skip gracefully under `set -euo pipefail`.
# Args: session entity extra_json field
#   extra_json: extra key/values appended inside the arguments object
#               (e.g. ',"criteria":"..."'), or empty.
# ---------------------------------------------------------------------------
mcp_first_field() {
  local session=$1 entity=$2 extra=$3 field="${4:-id}"
  local attempt resp val
  for attempt in 1 2 3; do
    resp=$(mcp_call "$session" "shopware-entity-search" "{\"entity\":\"${entity}\",\"limit\":1${extra}}")
    val=$( { echo "$resp" | ENTITY_FIELD="$field" python3 -c "
import json,sys,os
d=json.load(sys.stdin)
items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[])
print(items[0].get(os.environ['ENTITY_FIELD'],'') if items else '')
" 2>/dev/null; } || echo "")
    [[ -n "$val" ]] && break
  done
  echo "$val"
}

# ---------------------------------------------------------------------------
# tools/list following nextCursor pagination — echoes space-separated names.
# Echoes PAGINATION_OVERFLOW after 20 pages (runaway guard), DUPLICATE_TOOLS
# if a name repeats across pages.
# ---------------------------------------------------------------------------
mcp_list_tools_paginated() {
  local session=$1
  local cursor="" page=0 names="" resp page_names params
  while : ; do
    page=$((page+1))
    if [[ $page -gt 20 ]]; then echo "PAGINATION_OVERFLOW"; return 0; fi
    if [[ -n "$cursor" ]]; then
      params="{\"cursor\":\"${cursor}\"}"
    else
      params="{}"
    fi
    resp=$(curl -s -X POST "${SW_BASE_URL}/api/_mcp" \
      -H "Content-Type: application/json" \
      -H "sw-access-key: ${SW_ACCESS_KEY}" \
      -H "sw-secret-access-key: ${SW_SECRET_ACCESS_KEY}" \
      -H "Mcp-Session-Id: ${session}" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/list\",\"params\":${params},\"id\":2}" 2>/dev/null)
    page_names=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(' '.join(t['name'] for t in d.get('result',{}).get('tools',[])))
" 2>/dev/null)
    for n in $page_names; do
      if echo " $names " | grep -q " $n "; then echo "DUPLICATE_TOOLS:$n"; return 0; fi
      names="$names $n"
    done
    cursor=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('result',{}).get('nextCursor') or '')
" 2>/dev/null)
    [[ -z "$cursor" ]] && break
  done
  echo "$names"
}

# ---------------------------------------------------------------------------
# Assert: call a tool and check the response has no error and has content
# ---------------------------------------------------------------------------
assert_tool() {
  local session=$1 tool=$2 args=$3 label="${4:-$2}"
  local resp error content text

  resp=$(mcp_call "$session" "$tool" "$args")
  error=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error', {}).get('message', ''))" 2>/dev/null || echo "parse_error")
  content=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); c=d.get('result',{}).get('content',[]); print(len(c))" 2>/dev/null || echo "0")
  text=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('result',{}).get('content',[{}])[0].get('text','')[:120])" 2>/dev/null || echo "")

  if [[ -n "$error" ]]; then
    log_fail "$label" "$error"
    FAIL=$((FAIL+1))
    RESULTS=$(echo "$RESULTS" | python3 -c "
import json,sys
r=json.load(sys.stdin)
r.append({'tool':'${tool}','label':'${label}','status':'fail','error':'''${error}'''})
print(json.dumps(r))
")
  elif [[ "$content" == "0" ]]; then
    log_fail "$label" "empty content in response"
    FAIL=$((FAIL+1))
    RESULTS=$(echo "$RESULTS" | python3 -c "
import json,sys
r=json.load(sys.stdin)
r.append({'tool':'${tool}','label':'${label}','status':'fail','error':'empty content'})
print(json.dumps(r))
")
  else
    log_pass "$label"
    PASS=$((PASS+1))
    RESULTS=$(echo "$RESULTS" | python3 -c "
import json,sys
r=json.load(sys.stdin)
r.append({'tool':'${tool}','label':'${label}','status':'pass','preview':'''${text}'''})
print(json.dumps(r))
")
  fi
}

# ---------------------------------------------------------------------------
# Assert: call a tool and expect it to FAIL (protocol error, isError, or a
# {"success": false} payload), optionally containing a substring.
# ---------------------------------------------------------------------------
assert_tool_error() {
  local session=$1 tool=$2 args=$3 expected_substring=$4 label="${5:-$2 (error expected)}"
  local resp verdict

  resp=$(mcp_call "$session" "$tool" "$args")
  verdict=$(echo "$resp" | EXPECTED="$expected_substring" python3 -c "
import json,sys,os
d=json.load(sys.stdin)
expected=os.environ.get('EXPECTED','')
msg=d.get('error',{}).get('message','')
result=d.get('result',{})
text=(result.get('content',[{}]) or [{}])[0].get('text','')
is_error=bool(msg) or bool(result.get('isError'))
if not is_error:
    try:
        payload=json.loads(text)
        is_error=payload.get('success') is False
    except Exception:
        pass
if not is_error:
    print('NO_ERROR')
elif expected and expected not in (msg + text):
    print('WRONG_ERROR:' + (msg + ' ' + text)[:120])
else:
    print('OK')
" 2>/dev/null || echo "PARSE_ERROR")

  if [[ "$verdict" == "OK" ]]; then
    check_pass "$label"
  else
    check_fail "$label" "expected an error response, got: $verdict"
  fi
}

# ---------------------------------------------------------------------------
# v2: Default advertised surface — a fresh session must expose ONLY the three
# discovery meta-tools. Every catalogue tool is deferred; the model has to
# discover and enable what it needs. Any non-meta tool in the default list is a
# regression. Same on the admin and Store API endpoints.
DEFAULT_SURFACE=(
  shopware-tool-search shopware-toolsets-list shopware-toolset-enable
)

# The full catalogue is not hardcoded — verify_discovery_mechanics derives the
# expected set dynamically from shopware-toolsets-list (meta-tools + the union
# of every toolset's tools) and compares it against what is advertised after
# activating every toolset.

verify_default_surface() {
  local session=$1
  echo -e "\n${BOLD}v2: Default advertised surface${RESET}"

  local advertised
  advertised=$(mcp_list_tools_paginated "$session")
  case "$advertised" in
    PAGINATION_OVERFLOW*|DUPLICATE_TOOLS*)
      check_fail "tools/list pagination" "$advertised"
      return
      ;;
  esac

  local missing=() t
  for t in "${DEFAULT_SURFACE[@]}"; do
    if echo " $advertised " | grep -q " $t "; then
      check_pass "$t advertised by default"
    else
      check_fail "$t" "not in default tools/list"
      missing+=("$t")
    fi
  done

  # No extras: every advertised tool must be in DEFAULT_SURFACE
  local extras=""
  for t in $advertised; do
    if ! printf '%s\n' "${DEFAULT_SURFACE[@]}" | grep -qx "$t"; then
      extras="$extras $t"
    fi
  done
  if [[ -z "$extras" ]]; then
    check_pass "no deferred tools leak into the default surface"
  else
    check_fail "default surface" "unexpected tools advertised:$extras"
  fi
}

# ---------------------------------------------------------------------------
# v2: Toolset taxonomy — shopware-toolsets-list must expose the groups with
# complete metadata. Captures ENTITY_TOOLSET (the toolset containing
# shopware-entity-read) for the enable test.
# ---------------------------------------------------------------------------
ENTITY_TOOLSET=""
ALL_TOOLSETS=""
ALL_TOOLSET_TOOLS=""

verify_toolsets() {
  local session=$1
  echo -e "\n${BOLD}v2: Toolset taxonomy${RESET}"

  local resp summary
  resp=$(mcp_call "$session" "shopware-toolsets-list" '{}')
  summary=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=(d.get('result',{}).get('content',[{}]) or [{}])[0].get('text','{}')
payload=json.loads(text)
toolsets=payload.get('data',{}).get('toolsets',[])
problems=[]
required={'name','title','description','tools','enabled'}
union=set()
entity_toolset=''
for ts in toolsets:
    missing=required - set(ts.keys())
    if missing:
        problems.append(f\"{ts.get('name','?')} missing fields: {sorted(missing)}\")
    if ts.get('enabled') is not False:
        problems.append(f\"{ts.get('name','?')} enabled != false on fresh session\")
    union.update(ts.get('tools',[]))
    if 'shopware-entity-read' in ts.get('tools',[]):
        entity_toolset=ts['name']
print(json.dumps({
    'count': len(toolsets),
    'union': len(union),
    'problems': problems,
    'entity_toolset': entity_toolset,
    'names': ' '.join(sorted(ts['name'] for ts in toolsets)),
    'union_tools': ' '.join(sorted(union)),
}))
" 2>/dev/null || echo '{}')

  local count union problems entity_toolset
  count=$(echo "$summary" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))")
  union=$(echo "$summary" | python3 -c "import json,sys; print(json.load(sys.stdin).get('union',0))")
  problems=$(echo "$summary" | python3 -c "import json,sys; print('; '.join(json.load(sys.stdin).get('problems',['parse error'])))")
  ENTITY_TOOLSET=$(echo "$summary" | python3 -c "import json,sys; print(json.load(sys.stdin).get('entity_toolset',''))")
  ALL_TOOLSETS=$(echo "$summary" | python3 -c "import json,sys; print(json.load(sys.stdin).get('names',''))")
  ALL_TOOLSET_TOOLS=$(echo "$summary" | python3 -c "import json,sys; print(json.load(sys.stdin).get('union_tools',''))")

  if [[ "$count" -ge 8 ]]; then
    check_pass "toolsets-list returns $count toolsets (>= 8)"
  else
    check_fail "toolsets-list" "only $count toolsets returned"
  fi

  if [[ -z "$problems" ]]; then
    check_pass "every toolset has name/title/description/tools and enabled=false"
  else
    check_fail "toolset metadata" "$problems"
  fi

  if [[ "$union" -ge 18 ]]; then
    check_pass "toolsets cover $union deferred tools"
  else
    check_fail "toolset coverage" "union of toolset tools is only $union"
  fi

  if [[ -n "$ENTITY_TOOLSET" ]]; then
    check_pass "found toolset containing shopware-entity-read: $ENTITY_TOOLSET"
  else
    check_fail "entity toolset" "no toolset contains shopware-entity-read"
  fi
}

# ---------------------------------------------------------------------------
# v2: Discovery mechanics — enable grows the session's list, sessions are
# isolated, deferred tools stay directly callable, tool-search spans the full
# catalogue.
# ---------------------------------------------------------------------------
verify_discovery_mechanics() {
  echo -e "\n${BOLD}v2: Discovery mechanics${RESET}"

  local session_a session_b
  session_a=$(mcp_init)
  if [[ -z "$session_a" ]]; then
    check_fail "discovery session A" "mcp_init failed"
    return
  fi

  # --- toolset-enable returns listChanged and grows the advertised list ---
  if [[ -n "$ENTITY_TOOLSET" ]]; then
    local enable_resp list_changed
    enable_resp=$(mcp_call "$session_a" "shopware-toolset-enable" "{\"toolset\":\"${ENTITY_TOOLSET}\"}")
    list_changed=$(echo "$enable_resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=(d.get('result',{}).get('content',[{}]) or [{}])[0].get('text','{}')
payload=json.loads(text)
print('true' if payload.get('success') and payload.get('_meta',{}).get('listChanged') else 'false')
" 2>/dev/null || echo "false")
    if [[ "$list_changed" == "true" ]]; then
      check_pass "toolset-enable ($ENTITY_TOOLSET) succeeds with _meta.listChanged=true"
    else
      check_fail "toolset-enable ($ENTITY_TOOLSET)" "no success/listChanged in response"
    fi

    local advertised_a
    advertised_a=$(mcp_list_tools_paginated "$session_a")
    if echo " $advertised_a " | grep -q " shopware-entity-read "; then
      check_pass "shopware-entity-read advertised after enabling $ENTITY_TOOLSET"
    else
      check_fail "enable grows tools/list" "shopware-entity-read still not advertised"
    fi
    if echo " $advertised_a " | grep -q " shopware-tool-search "; then
      check_pass "default tools still advertised after enable"
    else
      check_fail "default tools after enable" "shopware-tool-search missing"
    fi
  else
    log_skip "toolset-enable test (no entity toolset found)"
    SKIP=$((SKIP+1))
  fi

  # --- session isolation: a new session must not see A's enabled toolsets ---
  session_b=$(mcp_init)
  if [[ -z "$session_b" ]]; then
    check_fail "discovery session B" "mcp_init failed"
    return
  fi
  local advertised_b
  advertised_b=$(mcp_list_tools_paginated "$session_b")
  if echo " $advertised_b " | grep -q " shopware-entity-read "; then
    check_fail "session isolation" "session B sees toolset enabled in session A"
  else
    check_pass "toolset enablement does not leak across sessions"
  fi

  # --- deferred tools stay directly callable (allowlist is the call boundary,
  #     advertising is not) ---
  assert_tool "$session_b" "shopware-system-config-read" \
    '{"key":"core.basicInformation"}' \
    "deferred tool callable without enable (system-config-read)"

  # --- tool-search: ranked results spanning deferred tools ---
  local search_resp search_verdict
  search_resp=$(mcp_call "$session_b" "shopware-tool-search" '{"query":"upload an image file","maxResults":5}')
  search_verdict=$(echo "$search_resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=(d.get('result',{}).get('content',[{}]) or [{}])[0].get('text','{}')
payload=json.loads(text)
results=payload.get('data',[])
meta=payload.get('_meta',{})
problems=[]
if not payload.get('success'): problems.append('success != true')
if not results: problems.append('no results')
for r in results:
    if not all(k in r for k in ('tool','score','matchedIn')):
        problems.append('result missing tool/score/matchedIn')
        break
names=[r.get('tool',{}).get('name','') for r in results]
if 'shopware-media-upload' not in names:
    problems.append(f'shopware-media-upload not in results: {names}')
if 'query' not in meta or 'totalCandidates' not in meta:
    problems.append('_meta missing query/totalCandidates')
print('OK' if not problems else '; '.join(problems))
" 2>/dev/null || echo "parse error")
  if [[ "$search_verdict" == "OK" ]]; then
    check_pass "tool-search finds deferred shopware-media-upload with score/matchedIn"
  else
    check_fail "tool-search (upload an image file)" "$search_verdict"
  fi

  # --- tool-search caps maxResults at 20 ---
  local cap_count
  cap_count=$(mcp_call "$session_b" "shopware-tool-search" '{"query":"shopware","maxResults":50}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=(d.get('result',{}).get('content',[{}]) or [{}])[0].get('text','{}')
print(len(json.loads(text).get('data',[])))
" 2>/dev/null || echo "-1")
  if [[ "$cap_count" -ge 1 && "$cap_count" -le 20 ]]; then
    check_pass "tool-search caps maxResults at 20 (got $cap_count)"
  else
    check_fail "tool-search maxResults cap" "expected 1..20 results, got $cap_count"
  fi

  # --- unknown toolset is rejected ---
  assert_tool_error "$session_b" "shopware-toolset-enable" \
    '{"toolset":"does-not-exist"}' "Unknown" \
    "toolset-enable rejects unknown toolset"

  # --- activate ALL toolsets: every catalogue tool must become reachable ---
  # Enables every toolset returned by shopware-toolsets-list, then compares the
  # advertised set against the expected full set computed *dynamically* from the
  # taxonomy (the 3 meta-tools plus the union of every toolset's tools). No
  # hardcoded list — this proves "enabling everything surfaces everything".
  local ts enabled_count=0
  for ts in $ALL_TOOLSETS; do
    local resp lc
    resp=$(mcp_call "$session_a" "shopware-toolset-enable" "{\"toolset\":\"${ts}\"}")
    lc=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=(d.get('result',{}).get('content',[{}]) or [{}])[0].get('text','{}')
print('1' if json.loads(text).get('success') else '0')
" 2>/dev/null || echo "0")
    [[ "$lc" == "1" ]] && enabled_count=$((enabled_count+1))
  done

  local n_toolsets
  n_toolsets=$(echo "$ALL_TOOLSETS" | wc -w | tr -d ' ')
  if [[ "$enabled_count" -eq "$n_toolsets" ]]; then
    check_pass "activated all $n_toolsets toolsets in one session"
  else
    check_fail "activate all toolsets" "only $enabled_count/$n_toolsets enable calls succeeded"
  fi

  # Expected full set = 3 meta-tools + union of all toolset tools.
  local expected_full expected_count advertised_full advertised_count
  expected_full=$(printf '%s\n' shopware-tool-search shopware-toolsets-list shopware-toolset-enable $ALL_TOOLSET_TOOLS | sort -u)
  expected_count=$(echo "$expected_full" | grep -c . )

  advertised_full=$(mcp_list_tools_paginated "$session_a")
  case "$advertised_full" in
    PAGINATION_OVERFLOW*|DUPLICATE_TOOLS*)
      check_fail "tools/list pagination after activate-all" "$advertised_full"
      return
      ;;
  esac
  advertised_count=$(echo "$advertised_full" | tr ' ' '\n' | grep -c . )

  # Missing: expected but not advertised. Extra: advertised but not expected.
  local missing="" extra="" t
  for t in $expected_full; do
    echo " $advertised_full " | grep -q " $t " || missing="$missing $t"
  done
  for t in $advertised_full; do
    echo "$expected_full" | grep -qx "$t" || extra="$extra $t"
  done

  if [[ -z "$missing" && -z "$extra" ]]; then
    check_pass "all tools reachable: $advertised_count/$expected_count advertised after activating every toolset (3 meta + $(echo "$ALL_TOOLSET_TOOLS" | wc -w | tr -d ' ') toolset tools)"
  else
    check_fail "activate-all completeness" "expected $expected_count, got $advertised_count; missing:${missing:- none}; extra:${extra:- none}"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo -e "${BOLD}Shopware MCP Functional Tests (v2 discovery)${RESET}"
echo "Server: ${SW_BASE_URL}"
echo ""

echo "Initializing MCP session..."
SESSION=$(mcp_init)
if [[ -z "$SESSION" ]]; then
  echo -e "${RED}ERROR: Failed to initialize MCP session. Check credentials.${RESET}"
  exit 1
fi
echo "Session: ${SESSION}"

verify_default_surface "$SESSION"
verify_toolsets "$SESSION"
verify_discovery_mechanics

# ---------------------------------------------------------------------------
# Per-tool assertions run on $SESSION with NO toolsets enabled — every call
# to a deferred tool below doubles as a direct-callability assertion
# (advertising narrows discovery, never permission).
# ---------------------------------------------------------------------------

# Fetch real IDs for read/upsert tests (with retry). A missing record yields ''
# and the `[[ -n "$X" ]]` guards below skip the dependent assertions.
PRODUCT_ID=$(mcp_first_field "$SESSION" "product" "" "id")
ORDER_ID=$(mcp_first_field "$SESSION" "order" "" "id")
CUSTOMER_EMAIL=$(mcp_first_field "$SESSION" "customer" "" "email")
SALES_CHANNEL_ID=$(mcp_first_field "$SESSION" "sales_channel" ',"criteria":"{\"filter\":[{\"type\":\"equals\",\"field\":\"typeId\",\"value\":\"8a243080f92e4c719546314b577cf82b\"}]}"' "id")

# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Core tools${RESET}"

assert_tool "$SESSION" "shopware-entity-schema" \
  '{"entity":"product"}' \
  "shopware-entity-schema (product)"

assert_tool "$SESSION" "shopware-entity-search" \
  '{"entity":"product","limit":1}' \
  "shopware-entity-search (product, limit 1)"

if [[ -n "$PRODUCT_ID" ]]; then
  assert_tool "$SESSION" "shopware-entity-read" \
    "{\"entity\":\"product\",\"id\":\"${PRODUCT_ID}\"}" \
    "shopware-entity-read (product by ID)"
else
  log_skip "shopware-entity-read (no product found)"
  SKIP=$((SKIP+1))
fi

assert_tool "$SESSION" "shopware-entity-aggregate" \
  '{"entity":"product","aggregations":"[{\"name\":\"total\",\"type\":\"count\",\"field\":\"id\"}]"}' \
  "shopware-entity-aggregate (count products)"

if [[ -n "$PRODUCT_ID" ]]; then
  assert_tool "$SESSION" "shopware-entity-upsert" \
    "{\"entity\":\"product\",\"payload\":\"{\\\"id\\\":\\\"${PRODUCT_ID}\\\",\\\"stock\\\":1}\",\"dryRun\":true}" \
    "shopware-entity-upsert (dryRun)"
else
  log_skip "shopware-entity-upsert (no product found)"
  SKIP=$((SKIP+1))
fi

assert_tool "$SESSION" "shopware-entity-delete" \
  '{"entity":"product","ids":"[\"00000000-0000-0000-0000-000000000000\"]","dryRun":true}' \
  "shopware-entity-delete (dryRun)"

assert_tool "$SESSION" "shopware-system-config-read" \
  '{"key":"core.basicInformation"}' \
  "shopware-system-config-read"

assert_tool "$SESSION" "shopware-system-config-write" \
  '{"key":"core.basicInformation.shopName","value":"\"Test\"","dryRun":true}' \
  "shopware-system-config-write (dryRun)"

if [[ -n "$ORDER_ID" ]]; then
  assert_tool "$SESSION" "shopware-order-state" \
    "{\"orderId\":\"${ORDER_ID}\",\"dryRun\":true}" \
    "shopware-order-state (dryRun)"
else
  log_skip "shopware-order-state (no order found)"
  SKIP=$((SKIP+1))
fi

if [[ "$SKIP_MEDIA_UPLOAD" == "true" ]]; then
  log_skip "shopware-media-upload (--skip-media-upload)"
  SKIP=$((SKIP+1))
else
  assert_tool "$SESSION" "shopware-media-upload" \
    '{"url":"https://assets.shopware.com/media/shopware_signet_blue.svg","fileName":"mcp-test-logo"}' \
    "shopware-media-upload"
fi

if [[ -n "$SALES_CHANNEL_ID" ]]; then
  assert_tool "$SESSION" "shopware-theme-config" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"action\":\"get\"}" \
    "shopware-theme-config (get)"
else
  log_skip "shopware-theme-config (no storefront sales channel found)"
  SKIP=$((SKIP+1))
fi

# ---------------------------------------------------------------------------
# Merchant tools
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Merchant tools${RESET}"

if [[ -n "$CUSTOMER_EMAIL" ]]; then
  assert_tool "$SESSION" "merchant-customer-lookup" \
    "{\"email\":\"${CUSTOMER_EMAIL}\"}" \
    "merchant-customer-lookup (by email)"
else
  log_skip "merchant-customer-lookup (no customer found)"
  SKIP=$((SKIP+1))
fi

if [[ -n "$ORDER_ID" ]]; then
  assert_tool "$SESSION" "merchant-order-summary" \
    "{\"orderId\":\"${ORDER_ID}\"}" \
    "merchant-order-summary (by ID)"
else
  log_skip "merchant-order-summary (no order found)"
  SKIP=$((SKIP+1))
fi

if [[ -n "$SALES_CHANNEL_ID" ]]; then
  assert_tool "$SESSION" "merchant-checkout-methods" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\"}" \
    "merchant-checkout-methods"

  assert_tool "$SESSION" "merchant-storefront-search" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"term\":\"shirt\"}" \
    "merchant-storefront-search (term: shirt)"

  # Create cart, capture token, then test checkout (dryRun)
  CART_RESP=$(mcp_call "$SESSION" "merchant-cart-manage" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"action\":\"create\"}")
  CART_TOKEN=$(echo "$CART_RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
text=d.get('result',{}).get('content',[{}])[0].get('text','{}')
try:
    r=json.loads(text)
    print(r.get('data',{}).get('token',''))
except:
    print('')
" 2>/dev/null || echo "")

  assert_tool "$SESSION" "merchant-cart-manage" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"action\":\"create\"}" \
    "merchant-cart-manage (create)"

  CUSTOMER_ID=$(mcp_first_field "$SESSION" "customer" "" "id")

  if [[ -n "$CART_TOKEN" && -n "$CUSTOMER_ID" ]]; then
    assert_tool "$SESSION" "merchant-cart-checkout" \
      "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"token\":\"${CART_TOKEN}\",\"customerId\":\"${CUSTOMER_ID}\",\"dryRun\":true}" \
      "merchant-cart-checkout (dryRun)"
  else
    log_skip "merchant-cart-checkout (could not get cart token or customer ID)"
    SKIP=$((SKIP+1))
  fi
else
  log_skip "merchant-checkout-methods (no storefront sales channel)"
  log_skip "merchant-storefront-search (no storefront sales channel)"
  log_skip "merchant-cart-manage (no storefront sales channel)"
  log_skip "merchant-cart-checkout (no storefront sales channel)"
  SKIP=$((SKIP+4))
fi

assert_tool "$SESSION" "merchant-product-create" \
  '{"name":"MCP Test Product","productNumber":"MCP-TEST-001","grossPrice":9.99,"dryRun":true}' \
  "merchant-product-create (dryRun)"

assert_tool "$SESSION" "merchant-bestseller-report" \
  '{"from":"2025-01-01","to":"2025-12-31","limit":5}' \
  "merchant-bestseller-report"

assert_tool "$SESSION" "merchant-revenue-report" \
  '{"from":"2025-01-01","to":"2025-12-31","groupBy":"month"}' \
  "merchant-revenue-report (groupBy month)"

# ---------------------------------------------------------------------------
# Dev tools
# ---------------------------------------------------------------------------
if [[ "$SKIP_DEV_TOOLS" == "true" ]]; then
  echo -e "\n${BOLD}Dev tools${RESET}"
  log_skip "dev tools (--skip-dev-tools)"
  SKIP=$((SKIP+1))
else
  echo -e "\n${BOLD}Dev tools${RESET}"

  assert_tool "$SESSION" "swag-dev-tools-log-search" \
    '{"query":"error","limit":5}' \
    "swag-dev-tools-log-search (query: error)"

  assert_tool "$SESSION" "swag-dev-tools-log-stream" \
    '{"limit":10}' \
    "swag-dev-tools-log-stream (last 10)"

  assert_tool "$SESSION" "swag-dev-tools-list-extensions" \
    '{}' \
    "swag-dev-tools-list-extensions"

  assert_tool "$SESSION" "swag-dev-tools-list-skills" \
    '{}' \
    "swag-dev-tools-list-skills"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL=$((PASS+FAIL+SKIP))
echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${RESET}, ${RED}${FAIL} failed${RESET}, ${YELLOW}${SKIP} skipped${RESET} / ${TOTAL} total"

mkdir -p "$(dirname "$RESULTS_FILE")"
echo "$RESULTS" | python3 -c "
import json,sys,os
from datetime import datetime,timezone
results=json.load(sys.stdin)
report={'timestamp':datetime.now(timezone.utc).isoformat(),'server':os.environ.get('SW_BASE_URL',''),'pass':${PASS},'fail':${FAIL},'skip':${SKIP},'total':${TOTAL},'tools':results}
print(json.dumps(report,indent=2))
" > "$RESULTS_FILE"
echo "Report: ${RESULTS_FILE}"

[[ "$FAIL" -eq 0 ]]
