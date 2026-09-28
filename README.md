# Org Chart Tool

Org-chart app for building and critiquing a client's structure: boxes for people and units,
notes, RAG flags (blue key-man · red broken/at-risk · grey undefined/to-hire), boards/versions,
team logins.

- **Backend:** FastAPI + Postgres 16 (`app/backend/`)
- **Frontend:** one self-contained HTML file (`app/frontend/index.html`) served by nginx
- **No client data in this repo** — the org lives in Postgres on the server only

## Run locally
```bash
cp .env.example .env && ln -s ../../.env app/backend/.env   # then edit the values
./scripts/build.sh                                          # tests + leak gate → dist/
cd app/backend && docker compose up -d --build              # db + api :8412 + web :3412
python3 ../../seed/build-seed.py                            # fictional example org
SEED_FILE=seed/example-org.json ADMIN_PW=<ADMIN_PASSWORD> ../../seed/push-seed.sh
```
Or open `app/frontend/index.html` directly for offline mode (browser storage only).

## Tests
```bash
node tests/run.js
```

See **CLAUDE.md** for architecture, the personal-data rules, and deployment decisions.
