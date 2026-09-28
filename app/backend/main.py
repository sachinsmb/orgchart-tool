"""
ACME MEP Org Assessment — FastAPI backend.

Replaces the frontend's localStorage layer with a real multi-user hosted API:
credentials to log in (JWT), and every note records who wrote it (author_id).

The API speaks the frontend's exact JSON shape. DB↔API column mapping:
  nodes.kind ↔ type · nodes.rag_status ↔ rag · nodes.cost_monthly ↔ cost ·
  color_defs.implied_action ↔ action.

Run locally:  docker compose up --build   (see README.md)
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from auth import create_token, current_user, hash_password, verify_password
from db import get_pool, lifespan
from models import (
    BoardIn,
    BoardPatchIn,
    CanvasIn,
    ColorIn,
    ImportIn,
    LoginIn,
    MoveIn,
    NodeCreateIn,
    PasswordChangeIn,
    NodePatchIn,
    NoteCreateIn,
    TagIn,
    NoteStatusIn,
    UserIn,
)

app = FastAPI(title="ACME MEP Org Assessment API", lifespan=lifespan)

# CORS: open for now. In prod, tighten `allow_origins` to the frontend origin
# (e.g. ["http://<tailscale-ip>:3412"]).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(asyncpg.exceptions.DataError)
async def _data_error(_request, exc: asyncpg.exceptions.DataError):
    # e.g. a malformed UUID in a path param → treat as "not found" rather than a 500.
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


# ---------------------------------------------------------------------------
# Serialisation helpers (DB row → frontend JSON shape)
# ---------------------------------------------------------------------------
def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # Normalise to a Z-suffixed UTC ISO string, matching the frontend.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _note_json(row: asyncpg.Record) -> dict[str, Any]:
    """A notes-join-users row → the frontend note shape."""
    return {
        "id": str(row["id"]),
        "author": row["author_name"] or "Unknown",
        "at": _iso(row["created_at"]),
        "body": row["body"],
        "sys": row["is_system"],
        "owner": row["owner"],
        "status": row["status"],
    }


def _color_json(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "key": row["key"],
        "label": row["label"],
        "hex": row["hex"],
        "meaning": row["meaning"],
        "action": row["implied_action"],
    }


def _node_json(row: asyncpg.Record) -> dict[str, Any]:
    """A nodes row → the frontend node shape (reports/notes filled in by caller)."""
    return {
        "id": str(row["id"]),
        "type": row["kind"],
        "name": row["name"],
        "title": row["title"],
        "entity": row["entity"],
        "rag": row["rag_status"],
        "tags": list(row["tags"] or []),
        "cost": float(row["cost_monthly"]) if row["cost_monthly"] is not None else None,
        "notes": [],
        "reports": [],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/auth/login")
async def login(body: LoginIn) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, email, role, password_hash FROM users WHERE email = $1",
            body.email.strip().lower() if "@" in body.email else body.email.strip(),
        )
    # Fall back to a case-sensitive exact match if the lowercase lookup missed.
    if row is None:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, email, role, password_hash FROM users WHERE email = $1",
                body.email.strip(),
            )
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")

    token = create_token(str(row["id"]))
    return {
        "token": token,
        "user": {
            "id": str(row["id"]),
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
        },
    }


@app.get("/me")
async def me(user: dict = Depends(current_user)) -> dict[str, Any]:
    return {
        "id": str(user["id"]),
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
    }


@app.get("/colors")
async def get_colors(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT key,label,hex,meaning,implied_action FROM color_defs "
            "ORDER BY sort_order, key"
        )
    return [_color_json(r) for r in rows]


@app.post("/colors")
async def add_color(body: ColorIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    key = _slug_key(body.key)                 # same XSS reasoning as tags: key lands in an on*="" handler
    if not key:
        raise HTTPException(status_code=400, detail="Colour key must be letters, numbers or dashes")
    async with get_pool().acquire() as conn:
        max_sort = await conn.fetchval("SELECT coalesce(max(sort_order),0) FROM color_defs")
        row = await conn.fetchrow(
            """
            INSERT INTO color_defs (key,label,hex,meaning,implied_action,sort_order)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (key) DO UPDATE SET
              label=EXCLUDED.label, hex=EXCLUDED.hex,
              meaning=EXCLUDED.meaning, implied_action=EXCLUDED.implied_action
            RETURNING key,label,hex,meaning,implied_action
            """,
            key, body.label, body.hex, body.meaning, body.action, (max_sort or 0) + 1,
        )
    return _color_json(row)


@app.delete("/colors/{key}")
async def delete_color(key: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Remove a colour. Any node currently rated with it is set back to unrated."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE nodes SET rag_status = NULL WHERE rag_status = $1", key)
            await conn.execute("DELETE FROM color_defs WHERE key = $1", key)
    return {"ok": True, "deleted": key}


# ---------------------------------------------------------------------------
# Boards — the as-is chart is the master; extra boards are an 'org' fork
# (restructure freely) or a 'blank' canvas (process mapping).
# ---------------------------------------------------------------------------
async def _master_board(conn) -> str:
    return await conn.fetchval(
        "SELECT id FROM boards WHERE is_master AND deleted_at IS NULL LIMIT 1"
    )


async def _resolve_board(conn, board: Optional[str]) -> str:
    """Board from the query string, falling back to master (keeps old callers working)."""
    if board:
        found = await conn.fetchval(
            "SELECT id FROM boards WHERE id = $1 AND deleted_at IS NULL", board
        )
        if found is None:
            raise HTTPException(404, "Board not found")
        return found
    return await _master_board(conn)


@app.get("/boards")
async def list_boards(deleted: bool = False, user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    """Live boards by default; `?deleted=true` lists the recycle bin for restoring."""
    where = "b.deleted_at IS NOT NULL" if deleted else "b.deleted_at IS NULL"
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT b.id, b.name, b.kind, b.is_master, b.created_at, b.deleted_at, "
            "(SELECT count(*) FROM nodes n WHERE n.board_id = b.id AND n.deleted_batch IS NULL) AS nodes "
            f"FROM boards b WHERE {where} ORDER BY b.is_master DESC, b.sort_order, b.created_at"
        )
    return [
        {"id": str(r["id"]), "name": r["name"], "kind": r["kind"],
         "is_master": r["is_master"], "nodes": r["nodes"],
         "created_at": _iso(r["created_at"]), "deleted_at": _iso(r["deleted_at"])}
        for r in rows
    ]


@app.post("/boards")
async def create_board(body: BoardIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Create a board. For an org copy, deep-clone the source tree.

    Copies structure + rag + tags but NOT notes: the as-is evidence stays on the
    as-is chart, and the to-be board starts a fresh discussion thread.
    """
    name = (body.name or "").strip() or "Untitled board"
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            nxt = await conn.fetchval("SELECT coalesce(max(sort_order),0)+1 FROM boards")
            board = await conn.fetchval(
                "INSERT INTO boards (name, kind, sort_order, created_by) "
                "VALUES ($1,$2,$3,$4) RETURNING id",
                name, body.kind, nxt, user["id"],
            )
            canvas: dict[str, Any] = {"pos": {}, "boxes": [], "links": []}

            if body.kind == "org" and body.copy_from:
                src = await conn.fetchval("SELECT id FROM boards WHERE id = $1", body.copy_from)
                if src is None:
                    raise HTTPException(404, "Source board not found")
                rows = await conn.fetch(
                    "SELECT id, parent_id, kind, name, title, entity, rag_status, tags, "
                    "cost_monthly, sort_order FROM nodes "
                    "WHERE board_id = $1 AND deleted_batch IS NULL ORDER BY sort_order, name",
                    src,
                )
                # New ids, then remap parents. Insert parents before children so
                # the self-FK is always satisfiable.
                idmap = {str(r["id"]): uuid.uuid4() for r in rows}
                byid = {str(r["id"]): r for r in rows}
                inserted: set[str] = set()

                def order(rid: str, seen: set[str]) -> list[str]:
                    if rid in inserted or rid in seen:
                        return []
                    seen.add(rid)
                    r = byid[rid]
                    pid = str(r["parent_id"]) if r["parent_id"] is not None else None
                    out = order(pid, seen) if (pid and pid in byid) else []
                    return out + [rid]

                seq: list[str] = []
                for rid in byid:
                    for x in order(rid, set()):
                        if x not in inserted:
                            inserted.add(x)
                            seq.append(x)

                for rid in seq:
                    r = byid[rid]
                    pid = str(r["parent_id"]) if r["parent_id"] is not None else None
                    await conn.execute(
                        "INSERT INTO nodes (id, board_id, parent_id, kind, name, title, entity, "
                        "rag_status, tags, cost_monthly, sort_order) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                        idmap[rid], board, idmap.get(pid) if pid else None, r["kind"], r["name"],
                        r["title"], r["entity"], r["rag_status"], list(r["tags"] or []),
                        r["cost_monthly"], r["sort_order"],
                    )

                # Carry the layout across, translated to the new node ids.
                srcdoc = await conn.fetchval(
                    "SELECT data FROM board_canvas WHERE board_id = $1", src
                )
                if srcdoc:
                    if isinstance(srcdoc, str):
                        srcdoc = json.loads(srcdoc or "{}")
                    canvas["pos"] = {
                        str(idmap[k]): v for k, v in (srcdoc.get("pos") or {}).items() if k in idmap
                    }
                    canvas["boxes"] = srcdoc.get("boxes") or []
                    canvas["links"] = srcdoc.get("links") or []

            await conn.execute(
                "INSERT INTO board_canvas (board_id, data) VALUES ($1,$2::jsonb)",
                board, json.dumps(canvas),
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM nodes WHERE board_id = $1", board
            )
    return {"id": str(board), "name": name, "kind": body.kind, "is_master": False, "nodes": count}


@app.patch("/boards/{board_id}")
async def rename_board(board_id: str, body: BoardPatchIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Board name required")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE boards SET name = $1 WHERE id = $2 RETURNING id", name, board_id
        )
    if row is None:
        raise HTTPException(404, "Board not found")
    return {"ok": True, "id": board_id, "name": name}


@app.delete("/boards/{board_id}")
async def delete_board(board_id: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        is_master = await conn.fetchval("SELECT is_master FROM boards WHERE id = $1", board_id)
        if is_master is None:
            raise HTTPException(404, "Board not found")
        if is_master:
            raise HTTPException(400, "The as-is board cannot be deleted")
        # Soft: keeps the board's nodes and canvas intact so it can be restored.
        await conn.execute(
            "UPDATE boards SET deleted_at = now() WHERE id = $1 AND deleted_at IS NULL", board_id
        )
    return {"ok": True, "deleted": board_id}


@app.post("/boards/{board_id}/restore")
async def restore_board(board_id: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Bring a deleted board back, with its structure and canvas as they were."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE boards SET deleted_at = NULL WHERE id = $1 RETURNING id, name", board_id
        )
    if row is None:
        raise HTTPException(404, "Board not found")
    return {"ok": True, "id": str(row["id"]), "name": row["name"]}


# ---------------------------------------------------------------------------
# Canvas — the shared whiteboard layer (node x/y, free boxes, connectors).
# Single JSONB document + version. Writes are compare-and-swap: a stale version
# gets a 409 so the client can merge rather than clobber a teammate's edit.
# ---------------------------------------------------------------------------
@app.get("/canvas")
async def get_canvas(board: Optional[str] = None, user: dict = Depends(current_user)) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        bid = await _resolve_board(conn, board)
        row = await conn.fetchrow(
            "SELECT data, version, updated_at FROM board_canvas WHERE board_id = $1", bid
        )
    if row is None:
        return {"data": {}, "version": 0, "updated_at": None}
    data = row["data"]
    if isinstance(data, str):          # asyncpg returns JSONB as text without a codec
        data = json.loads(data or "{}")
    return {"data": data or {}, "version": row["version"], "updated_at": _iso(row["updated_at"])}


@app.put("/canvas")
async def put_canvas(body: CanvasIn, board: Optional[str] = None, user: dict = Depends(current_user)) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        bid = await _resolve_board(conn, board)
        row = await conn.fetchrow(
            """
            UPDATE board_canvas
               SET data = $1::jsonb, version = version + 1,
                   updated_at = now(), updated_by = $2
             WHERE board_id = $4 AND version = $3
            RETURNING version
            """,
            json.dumps(body.data), user["id"], body.version, bid,
        )
    if row is None:
        # Someone else saved since this client last read. Not an error — the
        # client re-reads, merges its own changes in, and retries.
        raise HTTPException(status_code=409, detail="canvas-conflict")
    return {"ok": True, "version": row["version"]}


# ---------------------------------------------------------------------------
# Issue tags — an editable shared vocabulary, so roll-ups stay comparable
# across positions (free-text-per-node would fragment the counts).
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"[^a-z0-9]+")


def _slug_key(raw: str) -> str:
    """Normalise a tag/colour key to [a-z0-9-].

    These keys are rendered into on*="" handlers in the frontend, so anything
    outside this alphabet (quotes especially) is a stored-XSS vector. The client
    normalises too, but this is the authoritative check — the API is reachable
    directly, and every logged-in user (including client viewers) can post here.
    """
    return _KEY_RE.sub("-", str(raw or "").strip().lower()).strip("-")[:64]


@app.get("/tags")
async def get_tags(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    """Tags with their frozen definitions — the wording the client is held to."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, definition, test, action FROM tag_defs ORDER BY sort_order, name"
        )
    return [{"name": r["name"], "definition": r["definition"],
             "test": r["test"], "action": r["action"]} for r in rows]


@app.post("/tags")
async def add_tag(body: TagIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    name = _slug_key(body.name)
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Tag name must be 1-64 chars of lowercase letters, numbers or dashes",
        )
    async with get_pool().acquire() as conn:
        nxt = await conn.fetchval("SELECT coalesce(max(sort_order), 0) + 1 FROM tag_defs")
        await conn.execute(
            "INSERT INTO tag_defs (name, sort_order, definition, test, action) "
            "VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (name) DO UPDATE SET definition=coalesce(EXCLUDED.definition, tag_defs.definition), "
            "test=coalesce(EXCLUDED.test, tag_defs.test), action=coalesce(EXCLUDED.action, tag_defs.action)",
            name, nxt, body.definition, body.test, body.action,
        )
    return {"ok": True, "name": name}


@app.delete("/tags/{name}")
async def delete_tag(name: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Remove a tag from the vocabulary AND strip it off every node that carries it."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE nodes SET tags = array_remove(tags, $1)", name)
            await conn.execute("DELETE FROM tag_defs WHERE name = $1", name)
    return {"ok": True, "deleted": name}


# ---------------------------------------------------------------------------
# Users (team logins) — consultant role manages accounts; every login is a
# real person, so notes/changes are attributed to them.
# ---------------------------------------------------------------------------
def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "consultant":
        raise HTTPException(403, "This action needs a consultant (admin) account")
    return user


@app.patch("/me/password")
async def change_my_password(body: PasswordChangeIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Change your own password. Requires the current one, so a borrowed session cannot
    lock the real owner out."""
    if len(body.new) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1", user["id"]
        )
        if row is None or not verify_password(body.current, row["password_hash"]):
            # 403, not 401: the session is perfectly valid, it is the supplied password that
            # is wrong. The client force-logs-out on 401, so returning it here would sign the
            # user out for a typo in their own password change.
            raise HTTPException(403, "Current password is not correct")
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            hash_password(body.new), user["id"],
        )
    return {"ok": True}


@app.get("/users")
async def list_users(admin: dict = Depends(require_admin)) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, email, role FROM users ORDER BY created_at"
        )
    return [{"id": str(r["id"]), "name": r["name"], "email": r["email"], "role": r["role"]} for r in rows]


@app.post("/users")
async def create_user(body: UserIn, admin: dict = Depends(require_admin)) -> dict[str, Any]:
    email = body.email.strip().lower()
    if not email or not body.password:
        raise HTTPException(400, "Email and password are required")
    async with get_pool().acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM users WHERE email = $1", email):
            raise HTTPException(409, "A user with that email already exists")
        row = await conn.fetchrow(
            "INSERT INTO users (name, email, password_hash, role) VALUES ($1,$2,$3,$4) "
            "RETURNING id, name, email, role",
            (body.name.strip() or email), email, hash_password(body.password), body.role,
        )
    return {"id": str(row["id"]), "name": row["name"], "email": row["email"], "role": row["role"]}


@app.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: dict = Depends(require_admin)) -> dict[str, Any]:
    if str(admin["id"]) == user_id:
        raise HTTPException(400, "You cannot delete your own account")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # keep their content, just detach authorship, then remove the login
            await conn.execute("UPDATE notes SET author_id = NULL WHERE author_id = $1", user_id)
            await conn.execute("UPDATE audit SET actor_id = NULL WHERE actor_id = $1", user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
    return {"ok": True}


@app.get("/tree")
async def get_tree(board: Optional[str] = None, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Full nested tree in the frontend shape, including each node's notes."""
    async with get_pool().acquire() as conn:
        bid = await _resolve_board(conn, board)
        node_rows = await conn.fetch(
            "SELECT id, parent_id, kind, name, title, entity, rag_status, tags, "
            "cost_monthly, sort_order FROM nodes "
            "WHERE board_id = $1 AND deleted_batch IS NULL "
            "ORDER BY sort_order, name", bid
        )
        note_rows = await conn.fetch(
            """
            SELECT n.id, n.node_id, n.body, n.is_system, n.owner, n.status,
                   n.created_at, u.name AS author_name
            FROM notes n
            LEFT JOIN users u ON u.id = n.author_id
            ORDER BY n.created_at, n.id
            """
        )
        color_rows = await conn.fetch(
            "SELECT key,label,hex,meaning,implied_action FROM color_defs "
            "ORDER BY sort_order, key"
        )
        tag_rows = await conn.fetch(
            "SELECT name, definition, test, action FROM tag_defs ORDER BY sort_order, name"
        )

    # Build node objects keyed by id.
    nodes: dict[str, dict[str, Any]] = {}
    for r in node_rows:
        nodes[str(r["id"])] = _node_json(r)

    # Attach notes (already ordered by created_at).
    for r in note_rows:
        nid = str(r["node_id"])
        if nid in nodes:
            nodes[nid]["notes"].append(_note_json(r))

    # Assemble the adjacency list. Children preserve the SELECT order (sort_order, name).
    root: Optional[dict[str, Any]] = None
    for r in node_rows:
        nid = str(r["id"])
        pid = str(r["parent_id"]) if r["parent_id"] is not None else None
        if pid is None:
            root = nodes[nid]
        elif pid in nodes:
            nodes[pid]["reports"].append(nodes[nid])

    colors = [_color_json(r) for r in color_rows]
    return {"colors": colors, "root": root,
            "tags": [{"name": r["name"], "definition": r["definition"],
                      "test": r["test"], "action": r["action"]} for r in tag_rows]}


@app.post("/nodes")
async def create_node(body: NodeCreateIn, board: Optional[str] = None, user: dict = Depends(current_user)) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        bid = await _resolve_board(conn, board)
        if body.parent_id is not None:
            # A child always lands on its parent's board, whatever the caller said.
            bid = await conn.fetchval(
                "SELECT board_id FROM nodes WHERE id = $1 AND deleted_batch IS NULL", body.parent_id
            )
            if not bid:
                raise HTTPException(404, "parent_id not found")
            next_sort = await conn.fetchval(
                "SELECT coalesce(max(sort_order),-1)+1 FROM nodes WHERE parent_id = $1",
                body.parent_id,
            )
        else:
            # Creating a root — only allowed when there is no existing root.
            has_root = await conn.fetchval(
                "SELECT 1 FROM nodes WHERE parent_id IS NULL AND deleted_batch IS NULL "
                "AND board_id = $1 LIMIT 1", bid
            )
            if has_root:
                raise HTTPException(400, "A root node already exists; provide a parent_id")
            next_sort = 0

        row = await conn.fetchrow(
            """
            INSERT INTO nodes (parent_id, kind, name, title, entity, sort_order, board_id,
                               rag_status, tags)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id, parent_id, kind, name, title, entity, rag_status, tags,
                      cost_monthly, sort_order
            """,
            body.parent_id, body.type, body.name, body.title, body.entity, next_sort or 0, bid,
            body.rag, body.tags or [],
        )
    return _node_json(row)


@app.patch("/nodes/{node_id}")
async def patch_node(
    node_id: str, body: NodePatchIn, user: dict = Depends(current_user)
) -> dict[str, Any]:
    fields = body.model_fields_set  # only these were explicitly provided
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, kind, name, title, entity, rag_status, tags, cost_monthly "
                "FROM nodes WHERE id = $1 AND deleted_batch IS NULL FOR UPDATE",
                node_id,
            )
            if existing is None:
                raise HTTPException(404, "Node not found")

            sets: list[str] = []
            args: list[Any] = []

            def add_set(col: str, value: Any) -> None:
                args.append(value)
                sets.append(f"{col} = ${len(args)}")

            # Plain scalar fields.
            if "name" in fields:
                add_set("name", body.name)
            if "title" in fields:
                add_set("title", body.title)
            if "entity" in fields:
                add_set("entity", body.entity)

            # Audited fields: rag, tags, cost.
            if "rag" in fields and body.rag != existing["rag_status"]:
                # Validate the colour key exists (FK would reject anyway, but 400 is nicer).
                if body.rag is not None:
                    ok = await conn.fetchval(
                        "SELECT 1 FROM color_defs WHERE key = $1", body.rag
                    )
                    if not ok:
                        raise HTTPException(400, f"Unknown colour key: {body.rag}")
                add_set("rag_status", body.rag)
                await conn.execute(
                    "INSERT INTO audit (actor_id,node_id,field,old_value,new_value) "
                    "VALUES ($1,$2,'rag_status',$3,$4)",
                    user["id"], node_id, existing["rag_status"], body.rag,
                )
                # System note mirroring the frontend's setRag().
                label = "Unrated"
                if body.rag is not None:
                    label = await conn.fetchval(
                        "SELECT label FROM color_defs WHERE key = $1", body.rag
                    ) or body.rag
                await conn.execute(
                    "INSERT INTO notes (node_id,author_id,body,is_system) "
                    "VALUES ($1,$2,$3,true)",
                    node_id, user["id"], f"Status → {label}",
                )

            if "tags" in fields:
                new_tags = list(body.tags or [])
                old_tags = list(existing["tags"] or [])
                if new_tags != old_tags:
                    add_set("tags", new_tags)
                    await conn.execute(
                        "INSERT INTO audit (actor_id,node_id,field,old_value,new_value) "
                        "VALUES ($1,$2,'tags',$3,$4)",
                        user["id"], node_id,
                        "{" + ",".join(old_tags) + "}",
                        "{" + ",".join(new_tags) + "}",
                    )

            if "cost" in fields and _num(body.cost) != _num(existing["cost_monthly"]):
                add_set("cost_monthly", _dec(body.cost))
                await conn.execute(
                    "INSERT INTO audit (actor_id,node_id,field,old_value,new_value) "
                    "VALUES ($1,$2,'cost_monthly',$3,$4)",
                    user["id"], node_id,
                    _str_or_none(existing["cost_monthly"]), _str_or_none(body.cost),
                )

            if sets:
                args.append(node_id)
                await conn.execute(
                    f"UPDATE nodes SET {', '.join(sets)}, updated_at = now() "
                    f"WHERE id = ${len(args)}",
                    *args,
                )

            row = await conn.fetchrow(
                "SELECT id, parent_id, kind, name, title, entity, rag_status, tags, "
                "cost_monthly, sort_order FROM nodes WHERE id = $1 AND deleted_batch IS NULL",
                node_id,
            )
    return _node_json(row)


@app.delete("/nodes/{node_id}")
async def delete_node(node_id: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Soft-delete a node and its descendants, tagged with one batch id.

    Soft, not hard: a real DELETE cascades to child nodes, notes and audit rows,
    which makes the action unrecoverable. Marking keeps ids stable so a restore
    brings back the notes, history and canvas positions untouched. The returned
    `batch` is what the client hands back to undo exactly this delete.
    """
    batch = uuid.uuid4()
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            """
            WITH RECURSIVE sub AS (
              SELECT id FROM nodes WHERE id = $1 AND deleted_batch IS NULL
              UNION ALL
              SELECT n.id FROM nodes n JOIN sub s ON n.parent_id = s.id
              WHERE n.deleted_batch IS NULL
            )
            UPDATE nodes SET deleted_batch = $2 WHERE id IN (SELECT id FROM sub)
            """,
            node_id, batch,
        )
    if result.endswith(" 0"):
        raise HTTPException(404, "Node not found")
    return {"ok": True, "batch": str(batch), "count": int(result.rsplit(" ", 1)[1])}


@app.post("/nodes/restore/{batch}")
async def restore_nodes(batch: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Undo one delete batch — brings back the node, its reports, notes and history."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE nodes SET deleted_batch = NULL WHERE deleted_batch = $1", batch
        )
    return {"ok": True, "restored": int(result.rsplit(" ", 1)[1])}


@app.patch("/nodes/{node_id}/move")
async def move_node(
    node_id: str, body: MoveIn, user: dict = Depends(current_user)
) -> dict[str, Any]:
    new_parent = body.new_parent_id
    if new_parent == node_id:
        raise HTTPException(400, "A node cannot be its own parent")

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            node = await conn.fetchval("SELECT 1 FROM nodes WHERE id = $1 AND deleted_batch IS NULL", node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            parent = await conn.fetchval("SELECT 1 FROM nodes WHERE id = $1 AND deleted_batch IS NULL", new_parent)
            if not parent:
                raise HTTPException(404, "new_parent_id not found")

            # Cycle check: new_parent must not be a descendant of node.
            is_descendant = await conn.fetchval(
                """
                WITH RECURSIVE sub AS (
                    SELECT id FROM nodes WHERE id = $1
                    UNION ALL
                    SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id
                )
                SELECT 1 FROM sub WHERE id = $2
                """,
                node_id, new_parent,
            )
            if is_descendant:
                raise HTTPException(400, "Cannot move a node under one of its own descendants")

            next_sort = await conn.fetchval(
                "SELECT coalesce(max(sort_order),-1)+1 FROM nodes WHERE parent_id = $1",
                new_parent,
            )
            row = await conn.fetchrow(
                "UPDATE nodes SET parent_id = $1, sort_order = $2, updated_at = now() "
                "WHERE id = $3 "
                "RETURNING id, parent_id, kind, name, title, entity, rag_status, tags, "
                "cost_monthly, sort_order",
                new_parent, next_sort or 0, node_id,
            )
    return _node_json(row)


@app.post("/nodes/{node_id}/notes")
async def add_note(
    node_id: str, body: NoteCreateIn, user: dict = Depends(current_user)
) -> dict[str, Any]:
    status = "open" if body.is_action else None
    async with get_pool().acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM nodes WHERE id = $1 AND deleted_batch IS NULL", node_id)
        if not exists:
            raise HTTPException(404, "Node not found")
        row = await conn.fetchrow(
            """
            INSERT INTO notes (node_id, author_id, body, is_system, owner, status)
            VALUES ($1,$2,$3,false,$4,$5)
            RETURNING id, node_id, body, is_system, owner, status, created_at
            """,
            node_id, user["id"], body.body, body.owner, status,
        )
    return {
        "id": str(row["id"]),
        "author": user["name"],
        "at": _iso(row["created_at"]),
        "body": row["body"],
        "sys": row["is_system"],
        "owner": row["owner"],
        "status": row["status"],
    }


@app.patch("/notes/{note_id}")
async def update_note_status(
    note_id: str, body: NoteStatusIn, user: dict = Depends(current_user)
) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE notes SET status = $1 WHERE id = $2 "
            "RETURNING id, status", body.status, note_id,
        )
    if row is None:
        raise HTTPException(404, "Note not found")
    return {"id": str(row["id"]), "status": row["status"]}


@app.post("/import")
async def import_tree(body: ImportIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    """
    Replace the whole org with a frontend Export JSON ({colors, root}).

    Author attribution for imported notes: each note's `author` NAME is resolved
    to a users row; if no user with that name exists, a *placeholder* user is
    created (email `<slug>@import.acmemep.local`, an unusable password hash) so
    attribution is preserved verbatim. Notes with no author fall back to the
    importing user. See README for the rationale.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Wipe existing org (audit cascades via node_id FK; notes cascade too).
            await conn.execute("TRUNCATE nodes, notes, audit RESTART IDENTITY CASCADE")

            # 2. Upsert colours.
            if body.colors:
                for i, c in enumerate(body.colors):
                    key = c.get("key")
                    if not key:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO color_defs (key,label,hex,meaning,implied_action,sort_order)
                        VALUES ($1,$2,$3,$4,$5,$6)
                        ON CONFLICT (key) DO UPDATE SET
                          label=EXCLUDED.label, hex=EXCLUDED.hex,
                          meaning=EXCLUDED.meaning, implied_action=EXCLUDED.implied_action,
                          sort_order=EXCLUDED.sort_order
                        """,
                        key, c.get("label") or key, c.get("hex") or "#9ca3af",
                        c.get("meaning"), c.get("action"), i + 1,
                    )

            # Cache: author name (lowercased) → user id, to avoid re-querying.
            author_cache: dict[str, str] = {}

            async def resolve_author(name: Optional[str]) -> str:
                if not name or not name.strip():
                    return str(user["id"])
                key = name.strip().lower()
                if key in author_cache:
                    return author_cache[key]
                found = await conn.fetchval(
                    "SELECT id FROM users WHERE lower(name) = $1", key
                )
                if found is None:
                    slug = "".join(
                        ch if ch.isalnum() else "-" for ch in key
                    ).strip("-") or "user"
                    email = f"{slug}@import.acmemep.local"
                    found = await conn.fetchval(
                        """
                        INSERT INTO users (name, email, password_hash, role)
                        VALUES ($1, $2, '!', 'consultant')
                        ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
                        RETURNING id
                        """,
                        name.strip(), email,
                    )
                author_cache[key] = str(found)
                return str(found)

            # 3. Recursively insert the tree depth-first.
            valid_colors = {
                r["key"] for r in await conn.fetch("SELECT key FROM color_defs")
            }

            # Imported nodes belong to the master board. /tree filters on board_id, so leaving
            # it NULL makes a successful import invisible — the tree comes back empty.
            board_id = await _master_board(conn)
            if board_id is None:
                raise HTTPException(500, "No master board to import into")

            async def insert_node(
                node: dict[str, Any], parent_id: Optional[str], sort: int
            ) -> None:
                rag = node.get("rag")
                if rag is not None and rag not in valid_colors:
                    rag = None  # drop unknown colour rather than fail the FK
                cost = _dec(node.get("cost"))
                new_id = await conn.fetchval(
                    """
                    INSERT INTO nodes
                      (parent_id, kind, name, title, entity, rag_status, tags,
                       cost_monthly, sort_order, board_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING id
                    """,
                    parent_id,
                    node.get("type") or "person",
                    node.get("name") or "(unnamed)",
                    node.get("title"),
                    node.get("entity"),
                    rag,
                    list(node.get("tags") or []),
                    cost,
                    sort,
                    board_id,
                )
                # Notes for this node.
                for note in node.get("notes") or []:
                    author_id = await resolve_author(note.get("author"))
                    note_status = note.get("status")
                    if note_status not in ("open", "resolved"):
                        note_status = None
                    created = _parse_ts(note.get("at"))
                    if created is not None:
                        await conn.execute(
                            """
                            INSERT INTO notes
                              (node_id, author_id, body, is_system, owner, status, created_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7)
                            """,
                            new_id, author_id, note.get("body") or "",
                            bool(note.get("sys")), note.get("owner"),
                            note_status, created,
                        )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO notes
                              (node_id, author_id, body, is_system, owner, status)
                            VALUES ($1,$2,$3,$4,$5,$6)
                            """,
                            new_id, author_id, note.get("body") or "",
                            bool(note.get("sys")), note.get("owner"), note_status,
                        )
                # Children.
                for i, child in enumerate(node.get("reports") or []):
                    await insert_node(child, str(new_id), i)

            await insert_node(body.root, None, 0)

            counts = await conn.fetchrow(
                "SELECT (SELECT count(*) FROM nodes WHERE deleted_batch IS NULL) AS nodes, "
                "(SELECT count(*) FROM notes) AS notes"
            )
    return {"ok": True, "nodes": counts["nodes"], "notes": counts["notes"]}


# ---------------------------------------------------------------------------
# Small internal helpers
# ---------------------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _dec(v: Any) -> Optional[Decimal]:
    """Coerce a cost value → Decimal for asyncpg's numeric codec (rejects raw float)."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _str_or_none(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp string (with optional trailing Z) → aware datetime."""
    if not value or not isinstance(value, str):
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
