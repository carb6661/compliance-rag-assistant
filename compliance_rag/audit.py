"""Transactional maintenance evidence; not a security log for rejected attempts or WORM storage."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_EVENTS = 10_000


class AuditCapacityError(Exception):
    pass


class AuditCursorError(ValueError):
    pass


@dataclass(frozen=True)
class AuditActor:
    kind: str = "local"
    principal_id: str | None = None

    def __post_init__(self):
        if self.kind == "local" and self.principal_id is None:
            return
        if (self.kind == "api" and isinstance(self.principal_id, str)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", self.principal_id)):
            return
        raise ValueError("invalid server-side audit actor")


def validated_actor(actor):
    if actor is None:
        return AuditActor()
    if not isinstance(actor, AuditActor):
        raise ValueError("server-side audit actor required")
    return actor


class AuditCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    after_seq: int = Field(ge=0, le=2**63-1)
    through_seq: int = Field(ge=0, le=2**63-1)

    @model_validator(mode="after")
    def ordered(self):
        if self.after_seq > self.through_seq:
            raise ValueError("invalid audit cursor bounds")
        return self


class AuditPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: AuditCursor | None = None


def migrate(db):
    # Called inside KnowledgeStore's existing BEGIN IMMEDIATE; no executescript here.
    db.execute("CREATE TABLE maintenance_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "revision INTEGER NOT NULL UNIQUE, at_utc TEXT NOT NULL, actor_kind TEXT NOT NULL, "
        "actor_id TEXT, action TEXT NOT NULL, source TEXT NOT NULL, before_state TEXT, after_state TEXT)")
    db.execute("INSERT INTO settings VALUES ('audit_start_revision', "
               "(SELECT value FROM settings WHERE key='revision'))")
    db.execute("UPDATE settings SET value='4' WHERE key='schema'")


def state(store, db, row):
    if row is None:
        return None
    access = store._access_snapshot(db, row)
    return {"source_etag": access["source_etag"], "source_sha256": row["source_hash"],
        "config_sha256": row["config_hash"], "source_generation": row["generation"],
        "access_etag": access["access_etag"], "access_generation": row["access_generation"],
        "readers": access["readers"]}


def append(db, *, actor, action, source, revision, before, after):
    # A failed event write must abort the caller's source/ACL/revision transaction.
    if db.execute("SELECT count(*) FROM maintenance_events").fetchone()[0] >= MAX_EVENTS:
        raise AuditCapacityError("audit_capacity_exhausted")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db.execute("INSERT INTO maintenance_events "
        "(revision,at_utc,actor_kind,actor_id,action,source,before_state,after_state) VALUES (?,?,?,?,?,?,?,?)",
        (revision, timestamp, actor.kind, actor.principal_id, action, source,
         json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
         json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None))


def page(store, request=None):
    request = AuditPageRequest() if request is None else request
    if not isinstance(request, AuditPageRequest):
        raise ValueError("typed audit page request required")
    with store.connect() as db:
        db.execute("BEGIN")
        maximum = db.execute("SELECT coalesce(max(seq),0) FROM maintenance_events").fetchone()[0]
        cursor = request.cursor
        if cursor and (cursor.instance_id != store.instance_id or cursor.through_seq > maximum):
            raise AuditCursorError("audit cursor belongs to another or inconsistent snapshot")
        after, through = (cursor.after_seq, cursor.through_seq) if cursor else (0, maximum)
        rows = [dict(row) for row in db.execute("SELECT * FROM maintenance_events "
            "WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?", (after, through, request.limit+1))]
        floor = int(db.execute("SELECT value FROM settings WHERE key='audit_start_revision'").fetchone()[0])
    more = len(rows) > request.limit
    rows = rows[:request.limit]
    for row in rows:
        for key in ("before_state", "after_state"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
    return {"snapshot": {"instance_id": store.instance_id, "through_seq": through, "audit_start_revision": floor},
        "events": rows, "next_cursor": {"instance_id": store.instance_id,
            "after_seq": rows[-1]["seq"], "through_seq": through} if more else None}
