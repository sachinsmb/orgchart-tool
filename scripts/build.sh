#!/usr/bin/env bash
# Produce the file that actually gets served (dist/index.html).
#
# WHY THIS GATE EXISTS. This tool descends from one built for another engagement, where
# index.html carried 89 real employees with their assessment notes and the build stripped them
# before deploy. The HTML is served BEFORE authentication, so an unstripped build handed every
# name to anyone who opened the URL. That happened once, in production.
#
# Two things are different here:
#   1. The repo copy has no real people in it. ACME's org lives only in Postgres and reaches
#      the browser from /tree after login. The strip below is a no-op on a healthy tree, kept
#      because it still catches someone pasting real names between the markers.
#   2. The gate holds no plaintext names — of ACME's people or of the other engagement's.
#      It hashes every capitalised two-word sequence in the built file and compares against a
#      blocklist of digests. The digests live in scripts/name-blocklist.txt, which is gitignored:
#      a 16-hex digest of a short name can be recovered by hashing candidate names, so even the
#      hashes stay out of version control. Get the file from the DGX copy or the Master Log.
#      Without it the name check is skipped (with a warning); the other checks still run.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "  running tests…"
node tests/run.js | tail -1 | sed 's/^/  /'
node tests/run.js >/dev/null 2>&1 || { echo "BUILD REFUSED — tests failing."; exit 1; }

SRC=app/frontend/index.html
OUT=dist/index.html
mkdir -p dist

python3 - "$SRC" "$OUT" <<'PY'
import sys
src, out = sys.argv[1], sys.argv[2]
s = open(src).read()
start, end = "//__PEOPLE_START__", "//__PEOPLE_END__"
i, j = s.index(start), s.index(end)
placeholder = ("//__PEOPLE_START__ (empty by design — real org comes from the server after login)\n"
               "const SEED_ROOT = { id:'n1', type:'unit', name:'ACME MEP Services', title:'', entity:'',\n"
               "  rag:null, tags:[], notes:[], cost:null, _collapsed:false, reports:[] };\n")
n = s[i:j].count("P('")
open(out,'w').write(s[:i] + placeholder + s[j:])
print(f"  seed block: {n} person entries stripped" if n else "  seed block: already clean (0 person entries)")
PY

# --- the gate ---
python3 - "$OUT" <<'PY'
import sys, re, hashlib
# sha256(lowercased full name)[:16], one per line — see header for why the list is not in git.
import os
bl = os.environ.get("NAME_BLOCKLIST", "scripts/name-blocklist.txt")
try:
    BLOCK = {l.split("#")[0].strip() for l in open(bl)} - {""}
except OSError:
    BLOCK = set()
    print(f"  ⚠ {bl} missing — name check SKIPPED (assessment-text + person-entry checks still run)")
s = open(sys.argv[1]).read()
fail = []
for m in set(re.findall(r"\b[A-Z][a-z]{1,15}[ .]+[A-Z][a-z]{1,15}\b", s)):
    norm = re.sub(r"[ .]+", " ", m).strip().lower()
    if hashlib.sha256(norm.encode()).hexdigest()[:16] in BLOCK:
        fail.append(m)
if fail:
    for f in sorted(fail): print(f"  ✗ LEAK: a blocklisted person name is present ({len(f)} chars)")
    print("BUILD REFUSED — personal data would have been served."); sys.exit(1)
for phrase in ("Sole holder", "key-man risk flagged", "de-facto HR"):
    if phrase in s:
        print(f"  ✗ LEAK: assessment text '{phrase}' present"); sys.exit(1)
if s.count("P('"):
    print("  ✗ LEAK: person entries present"); sys.exit(1)
print(f"  ✓ gate: 0 of {len(BLOCK)} blocklisted names present · no assessment text · no person entries")
PY

# Hosted mode: set the API base so the Server URL field is auto-filled and hidden. Without it the
# served page sits in OFFLINE mode, where a real account password is rejected — which reads to the
# user as "login failed" and to us as a credential problem. It is neither; it is an unset global.
# This bit the first colleague to try signing in, on 2026-09-21.
python3 -c "
import sys
out = 'dist/index.html'
s = open(out).read()
if \"ORG_API_BASE='/api'\" in s:
    print('  ORG_API_BASE already present')
else:
    tag = \"<script>window.ORG_API_BASE='/api';</script>\"
    i = s.index('</head>') if '</head>' in s else s.index('<script>')
    open(out, 'w').write(s[:i] + tag + chr(10) + s[i:])
    print(\"  injected ORG_API_BASE='/api' (hosted mode - Server URL hidden)\")
"

# vendor assets travel INSIDE dist so the served tree is one directory. Nesting a second
# mount inside the read-only dist mount fails at container start with a read-only rootfs.
rsync -a --delete app/frontend/vendor/ dist/vendor/
echo "  ✓ vendor assets copied into dist/"

node -e 'const s=require("fs").readFileSync("dist/index.html","utf8");
  new Function(s.split("<script>")[2].split("</script>")[0]);' \
  || { echo "  ✗ built file is not valid JS"; exit 1; }

echo "  ✓ valid JS · $(wc -c < "$OUT" | tr -d " ") bytes"
