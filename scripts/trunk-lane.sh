#!/usr/bin/env bash
#
# Run the whole suite against a local trunk lane, before anything reaches CI.
#
# CI takes ~8 minutes and costs real money, and this session established the hard
# way that a local run and a CI run can disagree for reasons that have nothing to
# do with the code. Everything below is a lesson from that:
#
#   * the MCP rate limiter is ON by default locally and OFF in CI, so a suite
#     that paces a few hundred calls trips it and the client's own backoff makes
#     that look like a hang;
#   * the UCP profile URI is fetched BY THE SERVER, so it has to name a port the
#     server can reach — a shop published on :8088 through a host proxy listens
#     on :8000 inside its own container;
#   * the UCP journey commits, so it must never point at a shop anyone cares
#     about.
#
# Usage:
#   scripts/trunk-lane.sh                      # static checks + journey only, free
#   scripts/trunk-lane.sh --eval               # ... plus the LLM eval via LM Studio
#   scripts/trunk-lane.sh --eval --id catalog_search
#
# Configure with a .env or the environment:
#   SW_BASE_URL           the shop, e.g. http://trunk.localhost:8088
#   SW_SC_ACCESS_KEY      sales-channel access key for that shop
#   SW_ACCESS_KEY/SECRET  integration credentials, for the admin suite
#   UCP_PROFILE_URI       a profile URL the SERVER can fetch (see above)
#   UCP_JOURNEY_PROMO_CODE  a promotion code, or discount-apply skips
#   UCP_JOURNEY_CUSTOMER_EMAIL/_PASSWORD  the account the customer half of the
#                         journey shops as; registered on first use if absent
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
RUN_EVAL=false
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --eval) RUN_EVAL=true ;;
    *) EXTRA+=("$arg") ;;
  esac
done

# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a

: "${SW_BASE_URL:?set SW_BASE_URL to the trunk lane, e.g. http://trunk.localhost:8088}"

echo "=== lane: ${SW_BASE_URL}"
if [ -n "${UCP_PROFILE_URI:-}" ]; then
  echo "=== UCP profile (fetched by the server): ${UCP_PROFILE_URI}"
else
  echo "WARNING: UCP_PROFILE_URI is unset, so the server will fetch ${SW_BASE_URL}/.well-known/ucp."
  echo "         If the shop is published on a host-mapped port, the server cannot reach that and"
  echo "         every UCP tool fails with an opaque internal error. Set it to a port the server serves."
fi

# Preflight first: one direct call, no model, ~1s. It fails with a named cause,
# which is worth far more than the same failure discovered 45 fixtures later.
echo
echo "=== preflight (store)"
"${PYTHON}" -m eval.preflight --endpoint store

echo
echo "=== static checks + UCP buyer journey"
echo "NOTE: --allow-mutations places a REAL ORDER on ${SW_BASE_URL}."
"${PYTHON}" -m functional.runner --endpoint store --allow-mutations

echo
echo "=== static checks (admin)"
"${PYTHON}" -m functional.runner --endpoint admin || true

if [ "${RUN_EVAL}" != "true" ]; then
  echo
  echo "Done. Re-run with --eval to also run the LLM eval against LM Studio."
  exit 0
fi

# LM Studio serialises requests, so concurrency buys nothing and only risks
# timeouts. The point of this arm is to prove the harness works end to end, not
# to produce a number comparable with CI's.
echo
echo "=== LLM eval via LM Studio (${LMSTUDIO_BASE_URL:-http://127.0.0.1:1234/v1})"
"${PYTHON}" -m eval.runner --endpoint store --provider lmstudio \
  --discovery-concurrency 1 \
  --tool-health "results/tool-health-store.json" \
  --advisory "${EXTRA[@]}"
