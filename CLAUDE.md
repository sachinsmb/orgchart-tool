# Org Chart Tool

Interactive org-chart web app for building and critiquing a client's organisation structure.
**Hosted on the DGX Spark — no DigitalOcean, no incremental cost.**

> **This repo carries no client data.** No people, notes, CTC, logins or secrets. The org lives
> only in Postgres on the server. See [Personal data](#personal-data--read-before-changing-the-frontend).

| | |
|---|---|
| Access | `http://<dgx-tailscale-ip>:3412/` — **Tailscale must be connected**. This is the only access path, by decision. MagicDNS name of the DGX may also resolve. |
| Stack | FastAPI + Postgres 16 + nginx, `docker compose` |
| Ports | api **8412**, web **3412**. Postgres **not published** — nothing outside the compose network needs it |
| On the server | app dir set in `scripts/deploy.env` · containers `acmeorg-{db,api,web}` |
| Logins | created in the app (**Team & logins** modal). Initial passwords only in `team-logins.txt` (0600, on the server) |
| Secrets | generated **on the server** into `.env` (0600). Never in this repo, never in a transcript. Template: `.env.example` |

**Survives a reboot unattended**: `docker` and `tailscaled` enabled at boot; all three containers
are `restart: unless-stopped`.

### No public URL — decided 2026-09-22

There is deliberately **no domain, no DNS record and no tunnel hostname**. A hostname on the
firm's Cloudflare zone was built and staged, then dropped before it went live.

The requirement was a path the team does not depend on someone else for. Every candidate failed it:

| Candidate | Who can break it |
|---|---|
| A hostname on the firm's domain | whoever administers that Cloudflare zone |
| Tailscale Funnel — the no-domain option | **unavailable** on this node, needs a tailnet ACL change |
| A domain of our own on Cloudflare | nobody — but it costs money, or means migrating a zone that already carries live systems |
| **No DNS at all** | nobody, because there is no URL to break |

The last one was chosen: **no URL can be misconfigured because none exists**. The dependency moves
to tailnet membership, administered outside the team — a known and accepted trade.

**Do not add a tunnel hostname without revisiting that decision.**

## Why the DGX and not a droplet
The DGX already has Docker, disk, a tunnel and nginx. The trade is that **uptime becomes ours** — if
the DGX reboots or the network drops, the app is down and nobody else is on the hook. Acceptable for
an internal critiquing tool; revisit before it is shown to the client.

## Personal data — read before changing the frontend
This descends from a tool that **leaked in production**: the seeded org (real names plus assessment
notes) was compiled into `index.html`, which is served **before** authentication, so `curl` on the
public URL returned all of it.

Three things keep that from happening here:

1. **`app/frontend/index.html` contains no real people.** The `//__PEOPLE_START__` block is an empty
   root by design. The org reaches the browser from `/tree` after login.
2. **The leak gate holds no plaintext names.** `scripts/build.sh` hashes every capitalised two-word
   sequence in the built file and compares against SHA-256 digests in `scripts/name-blocklist.txt`.
   That file is **gitignored** — a 16-hex digest of a short name is recoverable by hashing candidate
   names. Without it the name check is skipped with a warning; the assessment-text and
   person-entry checks still run.
3. **Seed data is gitignored.** `seed/*.json` and any real seed generator stay out of git.
   `seed/build-seed.py` here builds a **fictional** org (`seed/example-org.json`).

`scripts/build.sh` **refuses the build** on a hit. Re-test after touching the gate — a gate nobody
has seen fire is a guess.

## Layout
```
orgchart-tool/
├── app/backend/      FastAPI — main.py · db.py · auth.py · models.py · schema.sql · compose
├── app/frontend/     index.html (single file, no real people) + vendor/
├── dist/             BUILT artifact — html + vendor, one self-contained dir. gitignored
├── seed/             build-seed.py → example-org.json (fictional) · push-seed.sh
├── scripts/          build.sh (gate) · deploy.sh (server) · deploy.env.example
└── tests/run.js      17 regression tests, gate the build
```

## Commands
```bash
node tests/run.js           # regression tests
./scripts/build.sh          # test + strip + gate + emit dist/
./scripts/deploy.sh         # build, rsync to server, compose up, verify  (needs scripts/deploy.env)
./scripts/deploy.sh status  # container state
./scripts/deploy.sh logs    # api log
./scripts/deploy.sh rollback
python3 seed/build-seed.py  # write the fictional seed/example-org.json
```
Seed **on the server** so the admin password never leaves it:
```bash
ssh <host> 'cd <app dir> && set -a && . ./.env && set +a && \
  API=http://localhost:8412 ADMIN_PW="$ADMIN_PASSWORD" bash seed/push-seed.sh'
```
`/import` **replaces the whole org**; `push-seed.sh` refuses if nodes already exist unless `--force`.

## Seeding discipline
Seed only what the interviews establish; do not invent reporting lines. Below the HOD layer, group
people by function and attach an open note saying the line is unconfirmed. RAG only where the
condition is documented: `blue` key-man, `red` broken or at risk of walking, `grey` undefined or
to-hire. An unset box is an honest box.

## Bugs fixed on the way in (09-21)
- **`/import` never set `board_id`** while `/tree` filters on it, so an import reported N nodes and
  returned an **empty tree**. Fixed in `main.py`. The upstream tool likely has the same latent bug.
- **`push-seed.sh`'s safety guard cried wolf** — it counted the six default *tags* as org nodes and
  blocked the first seed on an empty instance. Now counts org nodes by walking `.root` → `.reports`.
- **web container would not start** — `vendor/` was mounted *inside* the read-only `dist` mount.
  `dist/` is now one self-contained directory.

---

## Memory

This project has a **self-evolving memory** (`.memory/`): sessions are captured
automatically (SessionEnd/PreCompact hooks + a crash-safe sweeper), compiled
nightly into the wiki at `.memory/knowledge/`, and recalled as a brief injected
at session start. Rules: [.memory/MEMORY-RULES.md](.memory/MEMORY-RULES.md) —
binding for any agent writing to `knowledge/`. Deep search: `/recall <question>`.
Manual compile: `/memory-compile`. Trust the injected MEMORY BRIEF: check known
gotchas before re-deriving a fix. Only the vetted `knowledge/` bundle is committed.
