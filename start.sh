#!/usr/bin/env bash
# start.sh — bring up the PowerTrace observability stack and seed it with data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PowerTrace — Starting Observability Stack"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Start Docker Compose services ──────────────────────────────────────────
echo "▶  Starting Docker Compose services…"
# Support both docker compose (v2 plugin) and docker-compose (standalone)
if docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' found." >&2
  exit 1
fi
$COMPOSE up -d

echo ""
echo "   Waiting 20 s for services to become ready…"
sleep 20

# ── 2. Run correlation simulation ─────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "▶  Running PowerTrace correlation engine (simulate)…"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
python3 main.py simulate --output timeline

# ── 3. Export data to OTel stack ──────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "▶  Exporting data to OTel Collector + Grafana…"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
python3 export_to_otel.py

# ── 4. Print URLs ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Stack is ready."
echo ""
echo "  Grafana    →  http://localhost:3000"
echo "              username: admin"
echo "              password: powertrace"
echo ""
echo "  Prometheus →  http://localhost:9090"
echo ""
echo "  Dashboard  →  http://localhost:3000/d/powertrace-main"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
