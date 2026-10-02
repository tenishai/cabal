#!/usr/bin/env bash
# bootstrap.sh — one-shot setup after `docker compose -f docker-compose.dev.yml up -d`.
#
# What it does:
#   1. Waits for cabal to be healthy.
#   2. Extracts the bootstrap code from cabal container logs.
#   3. Exchanges the code for an operator JWT (saved to $CABAL_HOME/token,
#      default ~/.cabal/token -- the same file `cbl` reads).
#   4. Mints a secrets-fetch service token and writes it to LIMINAL_CABAL_TOKEN in .env.
#   5. Restarts liminal so it picks up the new token.
#
# Prereqs: docker, curl, python3 (any modern version), jq (optional — falls back to python3).
set -euo pipefail

CABAL_URL="${CABAL_URL:-http://localhost:7400}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
CABAL_CONTAINER="${CABAL_CONTAINER:-cabal-dev}"
ENV_FILE="${ENV_FILE:-.env}"

_json_field() {
    # Extract a JSON field from stdin. Uses jq if available, else python3.
    local field="$1"
    if command -v jq &>/dev/null; then
        jq -r ".$field"
    else
        python3 -c "import sys,json; print(json.load(sys.stdin)['$field'])"
    fi
}

echo "==> Waiting for cabal to be healthy at $CABAL_URL ..."
attempts=0
until curl -sf "$CABAL_URL/healthz" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -gt 60 ]; then
        echo "ERROR: cabal did not become healthy after 2 minutes."
        echo "Check logs: docker logs $CABAL_CONTAINER"
        exit 1
    fi
    printf "."
    sleep 2
done
echo " healthy."

echo "==> Extracting bootstrap code from $CABAL_CONTAINER logs..."
# structlog line looks like: ... cabal.bootstrap_code  code=XXXXXX msg='...'
CODE=$(docker logs "$CABAL_CONTAINER" 2>&1 \
    | grep "bootstrap_code" \
    | tail -1 \
    | grep -oE 'code=[A-Z0-9]+' \
    | cut -d= -f2)

if [ -z "$CODE" ]; then
    echo "ERROR: could not find bootstrap code in $CABAL_CONTAINER logs."
    echo "The code expires after 5 minutes — if cabal has been running longer, restart it:"
    echo "  docker compose -f $COMPOSE_FILE restart cabal"
    exit 1
fi
echo "Bootstrap code: $CODE"

echo "==> Exchanging code for operator token..."
# Capture the response first: piping a failed exchange straight into the JSON reader
# turned an expired code into a bare JSONDecodeError (tenishai/agora#47).
if ! RESP=$(curl -sf -X POST "$CABAL_URL/auth/bootstrap" \
    -H "Content-Type: application/json" \
    -d "{\"code\": \"$CODE\"}"); then
    echo "ERROR: cabal refused the bootstrap code. Codes expire 5 minutes after cabal starts"
    echo "and work once. Restart cabal for a fresh code, then run this again:"
    echo "  docker compose -f $COMPOSE_FILE restart cabal"
    exit 1
fi
OP_TOKEN=$(printf '%s' "$RESP" | _json_field access_token)

TOKEN_FILE="${CABAL_HOME:-$HOME/.cabal}/token"
mkdir -p "$(dirname "$TOKEN_FILE")"
echo "$OP_TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
echo "Operator token saved → $TOKEN_FILE"

echo "==> Minting secrets-fetch service token for liminal..."
SVC_TOKEN=$(curl -sf -X POST "$CABAL_URL/admin/token/mint-service" \
    -H "Authorization: Bearer $OP_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"sub":"service:liminal","scope":["secrets-fetch"],"aud":"cabal-core","ttl_days":365}' \
    | _json_field access_token)

echo "==> Writing LIMINAL_CABAL_TOKEN to $ENV_FILE ..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s|^LIMINAL_CABAL_TOKEN=.*|LIMINAL_CABAL_TOKEN=$SVC_TOKEN|" "$ENV_FILE"
else
    sed -i "s|^LIMINAL_CABAL_TOKEN=.*|LIMINAL_CABAL_TOKEN=$SVC_TOKEN|" "$ENV_FILE"
fi

echo "==> Restarting liminal with new token..."
docker compose -f "$COMPOSE_FILE" restart liminal

LIMINAL_URL="${LIMINAL_URL:-http://localhost:9100}"
echo "==> Waiting for liminal to be healthy at $LIMINAL_URL ..."
attempts=0
until curl -sf "$LIMINAL_URL/healthz" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -gt 60 ]; then
        echo "ERROR: liminal did not become healthy after 2 minutes."
        echo "Check logs: docker logs liminal-dev"
        exit 1
    fi
    printf "."
    sleep 2
done
echo " healthy."

echo ""
echo "Bootstrap complete."
echo ""
echo "Next steps:"
echo "  make demo                         # scratchpad round-trip smoke test"
echo "  cbl secret set <persona> <handler> KEY value  # store a secret"
echo "  CABAL_URL=$CABAL_URL cbl agents   # list registered agents"
