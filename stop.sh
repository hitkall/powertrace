#!/usr/bin/env bash
# stop.sh — tear down the PowerTrace observability stack
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PowerTrace — Stopping Observability Stack"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if docker compose version &>/dev/null 2>&1; then
  docker compose down
elif command -v docker-compose &>/dev/null; then
  docker-compose down
fi

echo ""
echo "  PowerTrace stack stopped."
echo ""
