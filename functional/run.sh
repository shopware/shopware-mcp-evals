#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Shopware MCP Functional Test Runner
# Requires: SW_BASE_URL, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY
# Usage: bash functional/run.sh [--skip-media-upload]
# ---------------------------------------------------------------------------

SKIP_MEDIA_UPLOAD=false
for arg in "$@"; do [[ "$arg" == "--skip-media-upload" ]] && SKIP_MEDIA_UPLOAD=true; done

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
# MCP session init — returns session ID via stdout
# ---------------------------------------------------------------------------
mcp_init() {
  local resp headers body session
  resp=$(curl -si -X POST "${SW_BASE_URL}/api/_mcp" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_ACCESS_KEY}" \
    -H "sw-secret-access-key: ${SW_SECRET_ACCESS_KEY}" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp-eval","version":"1.0"}},"id":1}' \
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
# Verify expected tools are registered
# ---------------------------------------------------------------------------
verify_tool_list() {
  local session=$1
  echo -e "\n${BOLD}Verifying tool registration${RESET}"

  local resp registered
  resp=$(curl -s -X POST "${SW_BASE_URL}/api/_mcp" \
    -H "Content-Type: application/json" \
    -H "sw-access-key: ${SW_ACCESS_KEY}" \
    -H "sw-secret-access-key: ${SW_SECRET_ACCESS_KEY}" \
    -H "Mcp-Session-Id: ${session}" \
    -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}' 2>/dev/null)

  registered=$(echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
tools=d.get('result',{}).get('tools',[])
print(' '.join(t['name'] for t in tools))
" 2>/dev/null)

  local expected=(
    shopware-entity-schema shopware-entity-search shopware-entity-read
    shopware-entity-aggregate shopware-entity-upsert shopware-entity-delete
    shopware-system-config-read shopware-system-config-write
    shopware-order-state shopware-media-upload shopware-theme-config
    merchant-customer-lookup merchant-order-summary merchant-cart-manage
    merchant-cart-checkout merchant-checkout-methods merchant-product-create
    merchant-storefront-search merchant-bestseller-report merchant-revenue-report
    swag-dev-tools-log-search swag-dev-tools-log-stream
  )

  local missing=()
  for t in "${expected[@]}"; do
    if echo "$registered" | grep -qw "$t"; then
      log_pass "$t registered"
      PASS=$((PASS+1))
    else
      log_fail "$t" "not found in tools/list"
      FAIL=$((FAIL+1))
      missing+=("$t")
    fi
  done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo -e "${BOLD}Shopware MCP Functional Tests${RESET}"
echo "Server: ${SW_BASE_URL}"
echo ""

echo "Initializing MCP session..."
SESSION=$(mcp_init)
if [[ -z "$SESSION" ]]; then
  echo -e "${RED}ERROR: Failed to initialize MCP session. Check credentials.${RESET}"
  exit 1
fi
echo "Session: ${SESSION}"

verify_tool_list "$SESSION"

# Fetch a real product ID for read/upsert tests
PRODUCT_ID=$(mcp_call "$SESSION" "shopware-entity-search" '{"entity":"product","limit":1}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[]); print(items[0]['id'] if items else '')" 2>/dev/null)

ORDER_ID=$(mcp_call "$SESSION" "shopware-entity-search" '{"entity":"order","limit":1}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[]); print(items[0]['id'] if items else '')" 2>/dev/null)

CUSTOMER_EMAIL=$(mcp_call "$SESSION" "shopware-entity-search" '{"entity":"customer","limit":1}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[]); print(items[0].get('email','') if items else '')" 2>/dev/null)

SALES_CHANNEL_ID=$(mcp_call "$SESSION" "shopware-entity-search" '{"entity":"sales_channel","criteria":"{\"filter\":[{\"type\":\"equals\",\"field\":\"typeId\",\"value\":\"8a243080f92e4c719546314b577cf82b\"}]}","limit":1}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[]); print(items[0]['id'] if items else '')" 2>/dev/null)

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
# Merchant assistant tools
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Merchant assistant tools${RESET}"

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
" 2>/dev/null)

  assert_tool "$SESSION" "merchant-cart-manage" \
    "{\"salesChannelId\":\"${SALES_CHANNEL_ID}\",\"action\":\"create\"}" \
    "merchant-cart-manage (create)"

  CUSTOMER_ID=$(mcp_call "$SESSION" "shopware-entity-search" '{"entity":"customer","limit":1}' \
    | python3 -c "import json,sys; d=json.load(sys.stdin); items=json.loads(d.get('result',{}).get('content',[{}])[0].get('text','{}')).get('data',[]); print(items[0]['id'] if items else '')" 2>/dev/null)

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
echo -e "\n${BOLD}Dev tools${RESET}"

assert_tool "$SESSION" "swag-dev-tools-log-search" \
  '{"query":"error","limit":5}' \
  "swag-dev-tools-log-search (query: error)"

assert_tool "$SESSION" "swag-dev-tools-log-stream" \
  '{"limit":10}' \
  "swag-dev-tools-log-stream (last 10)"

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
