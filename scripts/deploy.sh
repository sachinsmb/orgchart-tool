#!/usr/bin/env bash
# Deploy to the DGX Spark. No DigitalOcean droplet, no incremental hosting cost.
#
# Access is the Tailscale address only — no domain, no DNS record, no tunnel hostname.
# Decided 2026-09-22: a hostname on the firm's domain is a URL someone else can break, and
# every alternative (the firm's Cloudflare zone, Tailscale Funnel, the tailnet itself) also
# sits on infrastructure we do not administer. With no DNS there is no URL to break. The
# remaining dependency is tailnet membership, which is administered by someone else — that is
# a known and accepted trade, not an oversight.
#
#   ./scripts/deploy.sh            build, ship, bring the stack up, verify
#   ./scripts/deploy.sh list       releases on the DGX
#   ./scripts/deploy.sh rollback   put the previous frontend release back
#   ./scripts/deploy.sh status     container + health state
#   ./scripts/deploy.sh logs [n]   tail the api log
set -euo pipefail
cd "$(dirname "$0")/.."
# Target lives in scripts/deploy.env (gitignored) — copy scripts/deploy.env.example.
[ -f scripts/deploy.env ] && . scripts/deploy.env
: "${HOST:?set HOST in scripts/deploy.env (ssh alias of the server)}"
: "${DIR:?set DIR in scripts/deploy.env (app directory on the server)}"
: "${URL:?set URL in scripts/deploy.env (e.g. http://<tailscale-ip>:3412)}"
REL=$DIR/releases
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0 Safari/537.36'

case "${1:-deploy}" in
  list)
    ssh -o BatchMode=yes $HOST "ls -1t $REL 2>/dev/null | head -12; echo '--- live ---'; \
      md5sum $DIR/dist/index.html 2>/dev/null || echo '(nothing live)'"
    ;;
  status)
    ssh -o BatchMode=yes $HOST "cd $DIR && docker compose ps"
    ;;
  logs)
    ssh -o BatchMode=yes $HOST "cd $DIR && docker compose logs --tail=${2:-60} api"
    ;;
  rollback)
    ssh -o BatchMode=yes $HOST "set -e; prev=\$(ls -1t $REL | sed -n 2p); \
      [ -n \"\$prev\" ] || { echo 'no previous release'; exit 1; }; \
      cp $REL/\$prev $DIR/dist/index.html && echo \"rolled back to \$prev\""
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -H "User-Agent: $UA" "$URL/" || true)
    echo "  live HTTP $code"
    ;;
  deploy)
    ./scripts/build.sh
    STAMP=$(date +%Y%m%d-%H%M%S)
    ssh -o BatchMode=yes $HOST "mkdir -p $DIR $REL"
    # keep whatever is live before replacing it
    ssh -o BatchMode=yes $HOST "[ -f $DIR/dist/index.html ] && cp $DIR/dist/index.html $REL/pre-$STAMP.html || true"
    # ship source + built frontend. .env is never overwritten: secrets live only on the DGX.
    rsync -az --delete \
      --exclude '__pycache__/' --exclude '.env' \
      ./app ./dist ./seed "$HOST:$DIR/"
    ssh -o BatchMode=yes $HOST "cp $DIR/dist/index.html $REL/$STAMP.html; \
      ls -1t $REL | tail -n +21 | xargs -r -I{} rm -f $REL/{}"   # keep 20
    ssh -o BatchMode=yes $HOST "cd $DIR/app/backend && docker compose up -d --build" 2>&1 | tail -4
    sleep 4
    code=$(curl -s -o /tmp/.acmelive -w '%{http_code}' -m 25 -H "User-Agent: $UA" "$URL/" || true)
    # same hashed blocklist as build.sh (scripts/name-blocklist.txt, gitignored)
    leak=$(python3 - /tmp/.acmelive <<'PYGATE'
import sys, re, hashlib
import os
try: BLOCK = {l.split("#")[0].strip() for l in open("scripts/name-blocklist.txt")} - {""}
except OSError: BLOCK = set()
try: s = open(sys.argv[1]).read()
except OSError: print(0); sys.exit()
n = sum(1 for m in set(re.findall(r"\b[A-Z][a-z]{1,15}[ .]+[A-Z][a-z]{1,15}\b", s))
        if hashlib.sha256(re.sub(r"[ .]+"," ",m).strip().lower().encode()).hexdigest()[:16] in BLOCK)
print(n)
PYGATE
)
    rm -f /tmp/.acmelive
    echo "  released $STAMP · live HTTP $code · names exposed anonymously: $leak"
    [ "$leak" != "0" ] && { echo "  ✗ STILL LEAKING — run ./scripts/deploy.sh rollback"; exit 1; }
    echo "  ✓ deployed"
    ;;
esac
