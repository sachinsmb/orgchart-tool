#!/usr/bin/env bash
# Load a seed file into a running instance via POST /import.
# SEED_FILE defaults to seed/acme-org.json — the real seed, which is gitignored and exists only
# where it was generated. A fresh clone has seed/example-org.json (python3 seed/build-seed.py).
#
# /import REPLACES the whole org (it truncates nodes/notes/audit first), so this is a first-run
# or reset operation, never an update. Once the team starts editing in the browser, running this
# again discards their work. Guarded accordingly.
#
#   API=http://localhost:8412 ADMIN_PW=… ./seed/push-seed.sh
#   SEED_FILE=seed/example-org.json ./seed/push-seed.sh
#   ./seed/push-seed.sh --force      skip the "existing nodes" guard
set -euo pipefail
cd "$(dirname "$0")/.."
API="${API:-http://localhost:8412}"
EMAIL="${ADMIN_EMAIL:-admin@acmemep.local}"
PW="${ADMIN_PW:-${ADMIN_PASSWORD:-acmemep}}"
SEED_FILE="${SEED_FILE:-seed/acme-org.json}"
FORCE="${1:-}"
[ -f "$SEED_FILE" ] || { echo "✗ no seed at $SEED_FILE — set SEED_FILE (python3 seed/build-seed.py writes seed/example-org.json)"; exit 1; }

command -v jq >/dev/null || { echo "needs jq"; exit 1; }

TOKEN=$(curl -sS -m 20 -X POST "$API/auth/login" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg e "$EMAIL" --arg p "$PW" '{email:$e,password:$p}')" | jq -r '.token // empty')
[ -n "$TOKEN" ] || { echo "✗ login failed at $API as $EMAIL"; exit 1; }
echo "  logged in"

# Count ORG NODES ONLY — walk .root through .reports. A naive "objects with a name key"
# walk also counts the six default tags, so a fresh instance reports 6 and the guard blocks
# the very first seed. A guard that cries wolf teaches people to reach for --force.
EXISTING=$(curl -sS -m 20 "$API/tree" -H "Authorization: Bearer $TOKEN" \
  | jq '[.root | recurse(.reports[]?) | select(.name != null)] | length' 2>/dev/null || echo 0)
if [ "${EXISTING:-0}" -gt 1 ] && [ "$FORCE" != "--force" ]; then
  echo "✗ refusing: $EXISTING nodes already present. /import truncates everything."
  echo "  re-run with --force only if you mean to discard what is there."
  exit 1
fi

curl -sS -m 60 -X POST "$API/import" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' --data-binary @"$SEED_FILE" \
  | jq -r '"  imported: \(.nodes // "?") nodes, \(.notes // "?") notes"'
