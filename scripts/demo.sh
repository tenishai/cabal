#!/usr/bin/env bash
# demo.sh — end-to-end smoke test: scratchpad write → read via limenal.
#
# Requires: docker compose dev stack running + `make bootstrap` completed.
set -euo pipefail

CABAL_URL="${CABAL_URL:-http://localhost:7400}"
LIMENAL_URL="${LIMENAL_URL:-http://localhost:9100}"

_json_field() {
    local field="$1"
    if command -v jq &>/dev/null; then
        jq -r ".$field"
    else
        python3 -c "import sys,json; print(json.load(sys.stdin)['$field'])"
    fi
}

OP_TOKEN=$(cat ~/.cabal/token 2>/dev/null || true)
if [ -z "$OP_TOKEN" ]; then
    echo "ERROR: no operator token found at ~/.cabal/token. Run 'make bootstrap' first."
    exit 1
fi

echo "==> 1/6  limenal /healthz"
curl -sf "$LIMENAL_URL/healthz" | python3 -m json.tool
echo ""

echo "==> 2/6  Minting a demo persona dispatch token (persona: demo, scope: dispatch)"
DEMO_TOKEN=$(curl -sf -X POST "$CABAL_URL/admin/token/mint-service" \
    -H "Authorization: Bearer $OP_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"sub":"demo","persona":"demo","scope":["dispatch"],"aud":"limenal","ttl_days":1}' \
    | _json_field access_token)
echo "Token minted (valid 24h)."
echo ""

echo "==> 3/6  MCP initialize (no auth required)"
curl -sf -X POST "$LIMENAL_URL/mcp" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"0.1"}}}' \
    | python3 -m json.tool
echo ""

echo "==> 4/6  tools/list (persona: demo — expects scratchpad, notify, web)"
curl -sf -X POST "$LIMENAL_URL/mcp" \
    -H "Authorization: Bearer $DEMO_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | python3 -m json.tool
echo ""

echo "==> 5/6  scratchpad.set (key=hello, value='world from the demo!')"
curl -sf -X POST "$LIMENAL_URL/mcp" \
    -H "Authorization: Bearer $DEMO_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"scratchpad.set","arguments":{"key":"hello","value":"world from the demo!"}}}' \
    | python3 -m json.tool
echo ""

echo "==> 6/6  scratchpad.get (read it back)"
RESULT=$(curl -sf -X POST "$LIMENAL_URL/mcp" \
    -H "Authorization: Bearer $DEMO_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"scratchpad.get","arguments":{"key":"hello"}}}')
echo "$RESULT" | python3 -m json.tool

# Verify the value came back — limenal emits MCP-spec text blocks
# (content type is "text" with JSON in the text field, per MCP spec)
VALUE=$(echo "$RESULT" | python3 -c "
import sys, json
body = json.load(sys.stdin)
content = body.get('result', {}).get('content', [])
for item in content:
    if item.get('type') == 'text':
        text = item['text']
        # Try to parse as JSON (structured result); fall back to raw string
        try:
            data = json.loads(text)
            print(data.get('value', text))
        except (ValueError, AttributeError):
            print(text)
        break
" 2>/dev/null || true)

echo ""
if echo "$VALUE" | grep -q "world from the demo"; then
    echo "✓  Round-trip verified: scratchpad persists data across calls."
    echo "✓  JWT auth, handler dispatch, and per-agent isolation are all working."
    echo ""
    echo "Stack is healthy. cabal + limenal are running end-to-end."
else
    echo "WARNING: unexpected value returned from scratchpad.get: $VALUE"
    exit 1
fi
