#!/usr/bin/env bash
# test-docker.sh — full demo flow in a clean Ubuntu container.
#
# Mounts the three sibling repos into a fresh Ubuntu 22.04 container that
# shares the host Docker socket. Runs make up → bootstrap → demo → clean
# exactly as a new operator would. Proves the images build, services start,
# and the MCP round-trip works — all from a blank OS environment.
#
# Usage:
#   bash scripts/test-docker.sh
#   make test-docker
#
# Prereqs: Docker running, cabal/limenal/techne cloned as siblings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CABAL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LIMENAL_DIR="$(cd "$CABAL_DIR/../limenal" && pwd)"
TECHNE_DIR="$(cd "$CABAL_DIR/../techne" && pwd)"

# Bail early if the dev stack is already up — container names would conflict.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^cabal-dev$"; then
    echo "ERROR: cabal-dev container is already running."
    echo "Run 'make clean' to stop the dev stack before running the test."
    exit 1
fi

echo "==> Docker smoke test"
echo "    cabal:   $CABAL_DIR"
echo "    limenal: $LIMENAL_DIR"
echo "    techne:  $TECHNE_DIR"
echo ""

docker run --rm \
    --name "cabal-smoke-$$" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$CABAL_DIR":/workspace/cabal \
    -v "$LIMENAL_DIR":/workspace/limenal \
    -v "$TECHNE_DIR":/workspace/techne \
    -e HOST_CABAL_DIR="$CABAL_DIR" \
    -w /workspace/cabal \
    ubuntu:22.04 \
    bash -euo pipefail -c '
        echo "--- [1/5] install deps ---"
        apt-get update -qq 2>/dev/null
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            curl python3 make \
            docker.io docker-compose-v2 docker-buildx 2>/dev/null \
            | grep -E "^Setting up docker" || true
        docker compose version

        echo "--- [2/5] make up (build + start) ---"
        # Bind-mount paths are resolved by the HOST daemon, not the container.
        # Export the HOST-absolute identity path so compose can mount it correctly.
        export CABAL_IDENTITY_FILE="$HOST_CABAL_DIR/compose/identity.dev.toml"
        make up

        # localhost:7400 from inside this container hits our loopback, not the
        # HOST. Use the cabal/limenal container IPs directly — reachable from
        # any container on the same Docker host/VM without port mapping.
        _ip() { docker inspect "$1" | python3 -c \
          "import sys,json; n=json.load(sys.stdin)[0]['\''NetworkSettings'\'']['\''Networks'\'']; print(list(n.values())[0]['\''IPAddress'\''])"; }
        CABAL_IP=$(_ip cabal-dev)
        LIMENAL_IP=$(_ip limenal-dev)
        echo "  cabal IP:   $CABAL_IP"
        echo "  limenal IP: $LIMENAL_IP"
        export CABAL_URL="http://$CABAL_IP:7400"
        export LIMENAL_URL="http://$LIMENAL_IP:9100"

        echo "--- [3/5] make bootstrap ---"
        make bootstrap

        echo "--- [4/5] make demo ---"
        make demo

        echo "--- [5/5] make clean ---"
        make clean

        echo ""
        echo "✓  All steps passed — stack builds and runs end-to-end."
    '
