"""
asyncpg connection pool + FastAPI lifespan.

The pool is created on startup and exposed via `get_pool()` (used by a FastAPI
dependency in main.py). On first boot the DB schema is loaded by Postgres itself
(schema.sql mounted to /docker-entrypoint-initdb.d); this module only opens the
pool and, if `users` is empty, seeds the initial admin account.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from auth import hash_password

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://acme:acme@db:5432/acmeorg")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@acmemep.local")
ADMIN_NAME = os.getenv("ADMIN_NAME", "Admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "acmemep")

# Populated on startup, read via get_pool().
_pool: asyncpg.Pool | None = None


def get_pool() -> asyncpg.Pool:
    """Return the live pool. Raises if the app hasn't started yet."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


async def _seed_admin(pool: asyncpg.Pool) -> None:
    """Seed one admin user if the users table is empty."""
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM users")
        if count and count > 0:
            return
        await conn.execute(
            """
            INSERT INTO users (name, email, password_hash, role)
            VALUES ($1, $2, $3, 'consultant')
            ON CONFLICT (email) DO NOTHING
            """,
            ADMIN_NAME,
            ADMIN_EMAIL,
            hash_password(ADMIN_PASSWORD),
        )


# Frozen definitions. Each tag must be defensible to the client, so every one
# carries: what it means · the test that decides it · what we would do about it.
# Tag ≠ colour: the colour says "there is an issue", the tag says WHICH issue —
# overloaded and underutilised are both red but imply opposite interventions.
DEFAULT_TAG_DEFS = [
    ("overloaded",
     "Doing too many things. Looks efficient from the company's point of view in the short term, "
     "but degrades overall process efficiency over time.",
     "Decisions or actions queue behind this person, and throughput is capped by the hours they can "
     "personally give. Remove them for two weeks and the work slows rather than redistributes.",
     "Offload and redistribute work; codify the decisions they make by instinct so others can make them."),
    ("underutilised",
     "Carrying materially less scope than the role could hold. This is about MANDATE, not effort — "
     "it is not a judgement that someone is lazy.",
     "Output would not change much if their available time doubled; they own tasks rather than outcomes, "
     "and no part of the business would stall if they were absent.",
     "Expand the mandate and give them ownership of an outcome BEFORE considering replacement."),
    ("key-man",
     "The sole holder of a core process together with its tool and the undocumented knowledge to run it. "
     "Independent of performance — it is usually the strongest people.",
     "If they are unavailable for two weeks the process stops or materially degrades, and there is no "
     "documented way for anyone else to run it.",
     "Document the process, cross-train a named backup, move the knowledge out of the person into the system."),
    ("undefined-role",
     "No clear mandate, decision rights or boundaries. The person works from instruction rather than "
     "from a defined remit.",
     "Two people describe the job differently, and there is no clear answer to 'what can they decide "
     "on their own?'",
     "Define the role and its decision rights, then re-assess — most other labels are unreliable until this is fixed."),
    ("to-replace",
     "The role is right; the fit is wrong. The gap is capability or conduct — NOT workload and NOT an "
     "unclear mandate. The highest-sensitivity label: never apply it without evidence.",
     "After the mandate was clarified and the load corrected, performance still did not move.",
     "Plan a structured replacement. Confirm with the business owner before this is ever shown externally."),
    ("to-promote",
     "Consistently operating above the current remit. The constraint is the role, not the person.",
     "Already making decisions above their level and would be the natural owner of a larger scope today.",
     "Expand scope and title deliberately, rather than letting the scope creep informally."),
]
DEFAULT_TAGS = [t[0] for t in DEFAULT_TAG_DEFS]


async def _run_migrations(pool: asyncpg.Pool) -> None:
    """Idempotent startup migrations.

    Deliberately NOT in schema.sql: that file is mounted to
    /docker-entrypoint-initdb.d, which Postgres runs ONLY when the data
    directory is empty. Already-initialised deployments would never get these.
    """
    async with pool.acquire() as conn:
        # Soft delete. A hard DELETE cascades to child nodes, notes and audit
        # rows — unrecoverable, so deletes could never be undone. Marking the
        # rows keeps ids stable, which is what lets notes, history and the
        # client-side canvas layout survive a restore. Non-null = deleted; the
        # batch id groups one delete (a person + their reports) so restoring
        # brings back exactly that set.
        await conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS deleted_batch UUID")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_deleted ON nodes(deleted_batch)"
        )
        # Boards. The as-is chart is the master board; extra boards are either
        # an 'org' copy (a real fork of the tree — restructure freely without
        # touching as-is) or a 'blank' canvas (process mapping).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS boards (
              id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              name       TEXT NOT NULL,
              kind       TEXT NOT NULL DEFAULT 'org' CHECK (kind IN ('org','blank')),
              is_master  BOOLEAN NOT NULL DEFAULT false,
              sort_order INT NOT NULL DEFAULT 0,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              created_by UUID
            )
            """
        )
        await conn.execute(
            "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS board_id UUID "
            "REFERENCES boards(id) ON DELETE CASCADE"
        )
        # Boards soft-delete too. A hard DELETE cascades away the board's nodes
        # AND its canvas — same unrecoverable problem as deleting a person, and
        # a whole board is a much bigger thing to lose by mis-click.
        await conn.execute("ALTER TABLE boards ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        master = await conn.fetchval("SELECT id FROM boards WHERE is_master LIMIT 1")
        if master is None:
            master = await conn.fetchval(
                "INSERT INTO boards (name, kind, is_master, sort_order) "
                "VALUES ('As-is structure','org',true,0) RETURNING id"
            )
        # Backfill: every pre-existing node belongs to the master board.
        await conn.execute("UPDATE nodes SET board_id = $1 WHERE board_id IS NULL", master)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_board ON nodes(board_id)")

        # Canvas is per-board now. Carry the old single-row canvas_state over to
        # the master board once, so the shared layout built yesterday survives.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS board_canvas (
              board_id   UUID PRIMARY KEY REFERENCES boards(id) ON DELETE CASCADE,
              data       JSONB NOT NULL DEFAULT '{}'::jsonb,
              version    INT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_by UUID
            )
            """
        )
        # to_regclass guard: canvas_state only exists on a DB that ran the
        # previous migration. On a fresh install there is nothing to carry over.
        if await conn.fetchval("SELECT to_regclass('public.canvas_state')"):
            await conn.execute(
                "INSERT INTO board_canvas (board_id, data, version) "
                "SELECT $1, data, version FROM canvas_state WHERE id = 1 "
                "ON CONFLICT (board_id) DO NOTHING",
                master,
            )
        await conn.execute(
            "INSERT INTO board_canvas (board_id) VALUES ($1) ON CONFLICT DO NOTHING", master
        )

        # Whiteboard layer (node x/y, free boxes, connectors). Was per-browser
        # localStorage: invisible to teammates and absent from the DB backup.
        # One row of JSONB with a version counter for optimistic concurrency —
        # two people editing at once must not silently overwrite each other.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_state (
              id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
              data       JSONB NOT NULL DEFAULT '{}'::jsonb,
              version    INT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_by UUID
            )
            """
        )
        await conn.execute("INSERT INTO canvas_state (id) VALUES (1) ON CONFLICT DO NOTHING")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_defs (
              name       TEXT PRIMARY KEY,
              sort_order INT NOT NULL DEFAULT 0
            )
            """
        )
        for col in ("definition", "test", "action"):
            await conn.execute(f"ALTER TABLE tag_defs ADD COLUMN IF NOT EXISTS {col} TEXT")
        if not await conn.fetchval("SELECT count(*) FROM tag_defs"):
            await conn.executemany(
                "INSERT INTO tag_defs (name, sort_order) VALUES ($1,$2) "
                "ON CONFLICT (name) DO NOTHING",
                list(zip(DEFAULT_TAGS, range(len(DEFAULT_TAGS)))),
            )
        # Backfill the frozen wording onto the six standard tags wherever it is
        # still blank (existing deployments seeded them before definitions existed).
        for i, (name, definition, test, action) in enumerate(DEFAULT_TAG_DEFS):
            await conn.execute(
                "UPDATE tag_defs SET definition=coalesce(definition,$2), test=coalesce(test,$3), "
                "action=coalesce(action,$4), sort_order=$5 WHERE name=$1",
                name, definition, test, action, i,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the asyncpg pool, seed the admin user, hold it for the app's life."""
    global _pool
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)
    try:
        await _seed_admin(_pool)
        await _run_migrations(_pool)
        yield
    finally:
        await _pool.close()
        _pool = None
