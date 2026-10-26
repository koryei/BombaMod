#!/usr/bin/env bash
# Pull the latest code, rebuild, restart, and follow the logs. Run on the server.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "No .env found. Run ./scripts/setup-env.sh first."; exit 1; }
git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null
docker compose logs -f --tail 30 bombamod
