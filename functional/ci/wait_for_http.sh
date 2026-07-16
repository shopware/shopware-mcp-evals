#!/usr/bin/env bash
# Poll an HTTP endpoint until it returns an accepted status code.
#
# Reusable readiness gate for CI (and local use): waits for a service to come
# up before dependent steps run. Extracted from the workflow so it can be
# linted and reused instead of copy-pasted into `run:` blocks.
#
# Usage: wait_for_http.sh URL [ATTEMPTS] [SLEEP_SECONDS] [ACCEPTED_REGEX]
#   ACCEPTED_REGEX  extended-regex matched against the HTTP status code.
#                   Default '2..|401' — a 2xx or a 401 both prove the server is
#                   up (401 means the auth middleware is answering).
set -euo pipefail

URL=${1:?URL required}
ATTEMPTS=${2:-60}
SLEEP_SECONDS=${3:-2}
ACCEPTED=${4:-'2..|401'}

for ((i = 1; i <= ATTEMPTS; i++)); do
  status=$(curl -o /dev/null -w '%{http_code}' -sS --max-time 5 "$URL" 2>/dev/null || echo "000")
  if [[ "$status" =~ ^(${ACCEPTED})$ ]]; then
    echo "Ready (attempt $i, HTTP $status)"
    exit 0
  fi
  echo "Attempt $i: HTTP $status"
  sleep "$SLEEP_SECONDS"
done

echo "::error::$URL did not become ready after $ATTEMPTS attempts"
exit 1
