-- ACME MEP Org Assessment — Postgres schema.
-- Mirrors the frontend data model. Notes are append-only (satisfies "who added the note");
-- `body` is immutable once written, only `status` may change (resolve/reopen an action item).
-- rag/tag/cost changes are captured in `audit` so status history is reconstructable.
--
-- DB column ↔ API JSON mapping:
--   nodes.kind        ↔ node.type      ('person' | 'unit')
--   nodes.rag_status  ↔ node.rag       (colour key | null)
--   nodes.cost_monthly↔ node.cost      (monthly INR | null)
--   color_defs.implied_action ↔ color.action

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()

-- ---- Users (real credentials + attribution) ----
CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'consultant'   -- consultant | client_viewer
                CHECK (role IN ('consultant','client_viewer')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Colour definitions (legend is data → extensible palette) ----
CREATE TABLE IF NOT EXISTS color_defs (
  key            TEXT PRIMARY KEY,           -- green | amber | red | blue | grey | <custom>
  label          TEXT NOT NULL,
  hex            TEXT NOT NULL,
  meaning        TEXT,
  implied_action TEXT,                        -- ↔ API "action"
  sort_order     INT NOT NULL DEFAULT 0
);

-- ---- Org nodes (adjacency list: parent_id → tree) ----
CREATE TABLE IF NOT EXISTS nodes (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id    UUID REFERENCES nodes(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL DEFAULT 'person'  -- ↔ API "type"
               CHECK (kind IN ('person','unit')),
  name         TEXT NOT NULL,
  title        TEXT,
  entity       TEXT,                            -- Group | HO | GOA | BLR | UNI  (mirrors Growsmart branch tags M/G/B/U)
  rag_status   TEXT REFERENCES color_defs(key), -- NULL = unrated; ↔ API "rag"
  tags         TEXT[] NOT NULL DEFAULT '{}',
  cost_monthly NUMERIC,                         -- monthly cost (INR); ↔ API "cost"
  sort_order   INT NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

-- ---- Notes (append-only thread; body immutable once written) ----
-- A note is an "action item" when status IS NOT NULL. is_system=true → auto "Status → X".
CREATE TABLE IF NOT EXISTS notes (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  node_id    UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  author_id  UUID REFERENCES users(id),        -- who wrote it (nullable so a user can be pruned)
  body       TEXT NOT NULL,
  is_system  BOOLEAN NOT NULL DEFAULT false,    -- true for auto "Status → X" entries
  owner      TEXT,                              -- action-item owner (free text, e.g. "Kathan")
  status     TEXT CHECK (status IN ('open','resolved')),  -- non-null ⇒ this note is an action item
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notes_node ON notes(node_id, created_at);

-- ---- Audit (who changed rag/tags/cost on which node) ----
CREATE TABLE IF NOT EXISTS audit (
  id        BIGSERIAL PRIMARY KEY,
  actor_id  UUID REFERENCES users(id),
  node_id   UUID REFERENCES nodes(id) ON DELETE CASCADE,
  field     TEXT,                              -- rag_status | tags | cost_monthly
  old_value TEXT,
  new_value TEXT,
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_node ON audit(node_id, at);

-- Seed the default palette (matches the frontend COLORS array exactly).
INSERT INTO color_defs (key,label,hex,meaning,implied_action,sort_order) VALUES
  ('green','Strong','#22c55e','Well-utilised, performing','Keep / protect',1),
  ('amber','Attention','#f59e0b','Partial — some issue','Coach / clarify / monitor',2),
  ('red','Problem','#ef4444','Severely underutilised OR blocked from use','Replace, or load with more responsibility',3),
  ('blue','Key-man risk','#3b82f6','Single point of failure','Document, cross-train, build backup',4),
  ('grey','Vacant / undefined','#9ca3af','Role undefined or to-hire','Define + fill',5)
ON CONFLICT (key) DO NOTHING;
