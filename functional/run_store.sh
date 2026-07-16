#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Shopware Store API MCP Functional Test Runner (/store-api/_mcp)
#
# The Store API MCP endpoint uses the same v2 discovery mechanics as the admin
# endpoint, but authenticates with a SALES-CHANNEL access key (sw-access-key)
# plus a context token (sw-context-token) — not an admin integration key.
#
# This suite verifies the discovery mechanics, the store-api-context tool, and
# that the UCP buyer-journey tools become advertised once their toolset is
# enabled. It does NOT execute the UCP cart/checkout/catalog tools: those need
# a provisioned UCP catalog + live cart/checkout state, which a bare CI install
# does not have. Tool *selection* for those tools is covered by the LLM eval
# (eval/run.py --endpoint store).
#
# Requires: SW_BASE_URL, SW_SC_ACCESS_KEY
# Usage: bash functional/run_store.sh
# ---------------------------------------------------------------------------

[[ -f "$(dirname "$0")/../.env" ]] && source "$(dirname "$0")/../.env"

: "${SW_BASE_URL:?SW_BASE_URL is required}"
: "${SW_SC_ACCESS_KEY:?SW_SC_ACCESS_KEY is required (sales-channel access key)}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'
PASS=0; FAIL=0; SKIP=0
URL="${SW_BASE_URL}/store-api/_mcp"
CTX="$(openssl rand -hex 16)"

log_pass() { echo -e "  ${GREEN}PASS${RESET} $1"; PASS=$((PASS+1)); }
log_fail() { echo -e "  ${RED}FAIL${RESET} $1: $2"; FAIL=$((FAIL+1)); }
log_skip() { echo -e "  ${YELLOW}SKIP${RESET} $1"; SKIP=$((SKIP+1)); }

# init: returns a fresh Mcp-Session-Id (each call = a new session)
store_init() {
  curl -si -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_SC_ACCESS_KEY}" \
    -H "sw-context-token: ${CTX}" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp-eval-store","version":"2.0"}},"id":1}' \
    2>/dev/null | grep -i "^Mcp-Session-Id:" | tr -d '\r' | awk '{print $2}'
}

store_call() {
  local session=$1 tool=$2 args=$3
  curl -s -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_SC_ACCESS_KEY}" \
    -H "sw-context-token: ${CTX}" \
    -H "Mcp-Session-Id: ${session}" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"${tool}\",\"arguments\":${args}},\"id\":99}" \
    2>/dev/null
}

store_list() {
  local session=$1 cursor="" page=0 names="" resp params
  while : ; do
    page=$((page+1)); [[ $page -gt 20 ]] && { echo "PAGINATION_OVERFLOW"; return 0; }
    [[ -n "$cursor" ]] && params="{\"cursor\":\"${cursor}\"}" || params="{}"
    resp=$(curl -s -X POST "$URL" \
      -H "Content-Type: application/json" \
      -H "sw-access-key: ${SW_SC_ACCESS_KEY}" \
      -H "sw-context-token: ${CTX}" \
      -H "Mcp-Session-Id: ${session}" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/list\",\"params\":${params},\"id\":2}" 2>/dev/null)
    names="$names $(echo "$resp" | python3 -c "import json,sys;print(' '.join(t['name'] for t in json.load(sys.stdin).get('result',{}).get('tools',[])))" 2>/dev/null)"
    cursor=$(echo "$resp" | python3 -c "import json,sys;print(json.load(sys.stdin).get('result',{}).get('nextCursor') or '')" 2>/dev/null)
    [[ -z "$cursor" ]] && break
  done
  echo "$names"
}

echo -e "${BOLD}Shopware Store API MCP Functional Tests (v2 discovery)${RESET}"
echo "Endpoint: ${URL}"
echo ""

SESSION=$(store_init)
if [[ -z "$SESSION" ]]; then
  echo -e "${RED}ERROR: Store MCP initialize failed. Check SW_SC_ACCESS_KEY.${RESET}"
  exit 1
fi
echo "Session: ${SESSION}"

# --- default surface = the 3 meta-tools only ---
echo -e "\n${BOLD}v2: Default advertised surface${RESET}"
ADV=$(store_list "$SESSION")
for t in shopware-tool-search shopware-toolsets-list shopware-toolset-enable; do
  echo " $ADV " | grep -q " $t " && log_pass "$t advertised by default" || log_fail "$t" "not advertised"
done
EXTRAS=""
for t in $ADV; do
  case "$t" in shopware-tool-search|shopware-toolsets-list|shopware-toolset-enable) ;; *) EXTRAS="$EXTRAS $t";; esac
done
[[ -z "$EXTRAS" ]] && log_pass "no deferred tools leak into the store default surface" \
  || log_fail "store default surface" "unexpected advertised tools:$EXTRAS"

# --- toolsets taxonomy ---
echo -e "\n${BOLD}v2: Toolset taxonomy${RESET}"
TS_RESP=$(store_call "$SESSION" "shopware-toolsets-list" '{}')
TS_SUMMARY=$(echo "$TS_RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ts=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',{}).get('toolsets',[])
union=set()
ucp_ts=''
for t in ts:
    union.update(t.get('tools',[]))
    if any(n.startswith('shopware-ucp-') for n in t.get('tools',[])):
        ucp_ts=t['name']
print(json.dumps({'count':len(ts),'union':len(union),'ucp_toolset':ucp_ts}))
" 2>/dev/null || echo '{}')
TS_COUNT=$(echo "$TS_SUMMARY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('count',0))")
UCP_TOOLSET=$(echo "$TS_SUMMARY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('ucp_toolset',''))")
UCP_UNION=$(echo "$TS_SUMMARY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('union',0))")
[[ "$TS_COUNT" -ge 2 ]] && log_pass "toolsets-list returns $TS_COUNT toolsets (>= 2)" || log_fail "toolsets-list" "only $TS_COUNT"
[[ -n "$UCP_TOOLSET" ]] && log_pass "found UCP toolset: $UCP_TOOLSET" || log_fail "UCP toolset" "no toolset holds shopware-ucp-* tools"
[[ "$UCP_UNION" -ge 13 ]] && log_pass "toolsets cover $UCP_UNION deferred store tools" || log_fail "toolset coverage" "only $UCP_UNION"

# --- enable grows the list + listChanged; session isolation ---
echo -e "\n${BOLD}v2: Discovery mechanics${RESET}"
if [[ -n "$UCP_TOOLSET" ]]; then
  SESSION_A=$(store_init)
  EN=$(store_call "$SESSION_A" "shopware-toolset-enable" "{\"toolset\":\"${UCP_TOOLSET}\"}")
  echo "$EN" | python3 -c "import json,sys;d=json.load(sys.stdin);p=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}'));sys.exit(0 if p.get('success') and p.get('_meta',{}).get('listChanged') else 1)" 2>/dev/null \
    && log_pass "toolset-enable ($UCP_TOOLSET) → _meta.listChanged=true" \
    || log_fail "toolset-enable ($UCP_TOOLSET)" "no success/listChanged"
  ADV_A=$(store_list "$SESSION_A")
  echo " $ADV_A " | grep -q " shopware-ucp-cart-create " \
    && log_pass "UCP tools advertised after enabling $UCP_TOOLSET" \
    || log_fail "enable grows store tools/list" "shopware-ucp-cart-create not advertised"
  # session isolation
  SESSION_B=$(store_init)
  echo " $(store_list "$SESSION_B") " | grep -q " shopware-ucp-cart-create " \
    && log_fail "store session isolation" "session B sees A's enabled toolset" \
    || log_pass "toolset enablement does not leak across store sessions"
else
  log_skip "enable/isolation (no UCP toolset found)"
fi

# --- store-api-context: deferred but directly callable (allowlist boundary) ---
echo -e "\n${BOLD}Store context & search${RESET}"
SESSION_CTX=$(store_init)
CTX_RESP=$(store_call "${SESSION_CTX:-$SESSION}" "shopware-store-api-context" '{}')
echo "$CTX_RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}'))
data=p.get('data',{})
sys.exit(0 if p.get('success') and data.get('salesChannelId') and data.get('token') else 1)
" 2>/dev/null && log_pass "shopware-store-api-context (deferred, callable, returns channel+token)" \
  || log_fail "shopware-store-api-context" "missing salesChannelId/token or errored"

# --- tool-search finds a deferred UCP tool ---
# Fresh session: sessions share one context token and the server keeps only the
# most recent per token, so the original $SESSION may have been evicted by now.
SESSION_SEARCH=$(store_init)
SEARCH_RESP=$(store_call "${SESSION_SEARCH:-$SESSION}" "shopware-tool-search" '{"query":"add items to a shopping cart","maxResults":5}')
echo "$SEARCH_RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}'))
names=[r.get('tool',{}).get('name','') for r in p.get('data',[])]
ok = p.get('success') and any(n.startswith('shopware-ucp-cart') for n in names)
sys.exit(0 if ok else 1)
" 2>/dev/null && log_pass "shopware-tool-search finds a deferred UCP cart tool" \
  || log_fail "shopware-tool-search" "no UCP cart tool in results"

# ---------------------------------------------------------------------------
TOTAL=$((PASS+FAIL+SKIP))
echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${RESET}, ${RED}${FAIL} failed${RESET}, ${YELLOW}${SKIP} skipped${RESET} / ${TOTAL} total"
[[ "$FAIL" -eq 0 ]]
