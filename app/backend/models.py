"""
Pydantic v2 request bodies. Field names deliberately mirror the frontend JSON
shape (type/rag/cost) so the API is a drop-in swap for the localStorage layer.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str
    password: str


class NodeCreateIn(BaseModel):
    parent_id: Optional[str] = None          # None ⇒ create/replace the root
    type: Literal["person", "unit"] = "person"
    name: str
    title: Optional[str] = None
    entity: Optional[str] = None
    # Set at creation time by the New-position dialog. Pydantic drops unknown fields
    # silently, so a field the form sends but the model omits is lost with no error —
    # if the dialog gains a field, it has to be added HERE and to the INSERT below.
    rag: Optional[str] = None
    tags: list[str] = []


class NodePatchIn(BaseModel):
    # All optional — only provided fields are updated. Use model_fields_set to
    # distinguish "set to null" from "omitted".
    name: Optional[str] = None
    title: Optional[str] = None
    entity: Optional[str] = None
    rag: Optional[str] = None                 # colour key | null (unrated)
    tags: Optional[list[str]] = None
    cost: Optional[float] = None              # monthly INR | null


class MoveIn(BaseModel):
    new_parent_id: str


class NoteCreateIn(BaseModel):
    body: str
    owner: Optional[str] = None
    is_action: bool = False                   # true ⇒ status starts 'open'


class NoteStatusIn(BaseModel):
    status: Literal["open", "resolved"]


class ColorIn(BaseModel):
    key: str
    label: str
    hex: str
    meaning: Optional[str] = None
    action: Optional[str] = None              # ↔ DB implied_action


class ImportIn(BaseModel):
    colors: Optional[list[dict[str, Any]]] = None
    root: dict[str, Any] = Field(..., description="Nested tree in the frontend shape")


class BoardIn(BaseModel):
    name: str
    kind: Literal["org", "blank"] = "org"
    copy_from: Optional[str] = None           # board id to fork (org boards only)


class BoardPatchIn(BaseModel):
    name: str


class CanvasIn(BaseModel):
    data: dict[str, Any]                      # {pos:{nodeId:{x,y}}, boxes:[...], links:[...]}
    version: int                              # version the client last read; mismatch ⇒ 409


class TagIn(BaseModel):
    name: str                                 # free-text issue tag, e.g. "no-backup"
    definition: Optional[str] = None          # what it means — frozen wording
    test: Optional[str] = None                # the test that decides it
    action: Optional[str] = None              # what we would do about it


class UserIn(BaseModel):
    name: str
    email: str
    password: str
    role: Literal["consultant", "client_viewer"] = "consultant"


class PasswordChangeIn(BaseModel):
    current: str
    new: str
