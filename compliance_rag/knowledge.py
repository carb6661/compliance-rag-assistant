"""Private SQLite evidence store with revision-bound, credential-bound source readers."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import struct
import uuid

from .models import IngestConfig, StrictModel
from pydantic import Field, field_validator
from typing import Literal
from .parser import InputError, canonical_json, normalize_text, parse_markdown, source_label
from .retrieval import rank, unit_vector
from .conditions import WriteCondition, WriteRejected, require_condition
from .parser import sha
from .authorization import ReadIdentity, validated_grants


class Scope(StrictModel):
    standard_code: str = Field(min_length=1, max_length=128)
    edition: str = Field(min_length=1, max_length=128)
    security_level: Literal["第一级", "第二级", "第三级", "第四级", "第五级"]
    security_class: str | None = Field(default=None, min_length=1, max_length=128)


class SearchRequest(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    scope: Scope
    retrieval: Literal["bm25", "dense", "hybrid"] = "bm25"
    top_k: int = Field(default=3, ge=1, le=5, strict=True)

    @field_validator("question")
    @classmethod
    def question_valid(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("blank or NUL question")
        value.encode("utf-8")
        return value.strip()


class KnowledgeStore:
    def __init__(self, path, embedder=None, dense_min_cosine=0.50):
        if isinstance(dense_min_cosine, bool) or not isinstance(dense_min_cosine, (int, float)) or not math.isfinite(dense_min_cosine) or not 0 <= dense_min_cosine <= 1:
            raise ValueError("dense threshold must be finite and in 0..1")
        self.dense_min_cosine = dense_min_cosine
        self.path = Path(path)
        self.embedder = embedder
        self.identity = embedder.identity if embedder else "none"
        self.dimension = embedder.dimension if embedder else 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                  name TEXT PRIMARY KEY, source_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
                  normalized_text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                  chunk_id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
                  standard_code TEXT NOT NULL, edition TEXT NOT NULL, level TEXT NOT NULL,
                  security_class TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                  metadata TEXT NOT NULL, vector BLOB);
                CREATE INDEX IF NOT EXISTS scoped ON chunks(standard_code,edition,level,security_class);
                CREATE TABLE IF NOT EXISTS traces (
                  trace_id TEXT PRIMARY KEY, revision INTEGER, status TEXT, reason TEXT,
                  retrieval TEXT, generation TEXT, candidates INTEGER, citations INTEGER,
                  total_ms REAL);
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('revision','0')")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('embedding',?)", (self.identity,))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('dimension',?)", (str(self.dimension),))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('schema','1')")
            values = dict(db.execute("SELECT key,value FROM settings"))
            if values["embedding"] != self.identity or values["dimension"] != str(self.dimension) or values["schema"] not in ("1", "2", "3"):
                raise InputError("index identity/schema mismatch; use a separate database or explicit rebuild")
            if values["schema"] == "1":
                db.execute("ALTER TABLE sources ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE sources SET generation=?", (self._revision(db),))
                db.execute("INSERT INTO settings VALUES ('instance_id',?)", (uuid.uuid4().hex,))
                db.execute("UPDATE settings SET value='2' WHERE key='schema'")
            if values["schema"] in ("1", "2"):
                self._migrate_source_access(db)
            if not {"generation", "access_generation"} <= {row[1] for row in db.execute("PRAGMA table_info(sources)")}:
                raise InputError("source generation columns are missing")
            if not {"source", "principal_id", "credential_sha256"} <= {row[1] for row in db.execute("PRAGMA table_info(source_grants)")}:
                raise InputError("source reader table is missing or invalid")
            instance = db.execute("SELECT value FROM settings WHERE key='instance_id'").fetchone()
            if instance is None or len(instance[0]) != 32 or any(c not in "0123456789abcdef" for c in instance[0]):
                raise InputError("invalid knowledge store instance identity")
            self.instance_id = instance[0]

    @staticmethod
    def _migrate_source_access(db):
        # No broad grants on upgrade; original chunks, source ETags and revision stay intact.
        db.execute("ALTER TABLE sources ADD COLUMN access_generation INTEGER NOT NULL DEFAULT 0")
        db.execute("CREATE TABLE source_grants (source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE, "
                   "principal_id TEXT NOT NULL, credential_sha256 TEXT NOT NULL, PRIMARY KEY(source,principal_id))")
        db.execute("UPDATE settings SET value='3' WHERE key='schema'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def revision(self):
        with self.connect() as db:
            return int(db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0])

    def _source_etag(self, row):
        identity = [self.instance_id, row["name"], row["generation"], row["source_hash"], row["config_hash"]]
        return '"' + sha(canonical_json(identity).encode("utf-8")) + '"'

    def _check_condition(self, row, condition):
        matches = row is None if condition.mode == "create" else row is not None and self._source_etag(row) == condition.etag
        if not matches:
            # Do not return the newer token: a conflict must require an explicit fresh read.
            raise WriteRejected("precondition_failed", 412)

    def source(self, source_name):
        source_label(source_name)
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            if row is None:
                return None
            return {"source_name": row["name"], "source_etag": self._source_etag(row),
                    "source_generation": row["generation"],
                    "source_sha256": row["source_hash"], "config_sha256": row["config_hash"],
                    "normalized_text": row["normalized_text"]}

    def ingest(self, text, source_name, config=None, *, condition=None):
        source_label(source_name)
        condition = require_condition(condition)
        # Cheap preflight avoids parsing/embedding an already stale submission.
        # It is not the commit guard: the transaction repeats the check below.
        with self.connect() as db:
            self._check_condition(db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone(), condition)
        config = config or IngestConfig()
        result = parse_markdown(text, source_name, config)
        if result.errors:
            raise InputError("strict ingestion blocked; existing source is unchanged")
        if len(result.documents) > 2000:
            raise InputError("knowledge store chunk budget exceeded")
        texts = [" / ".join(d.metadata["标题路径"][-2:]) + "\n" + d.page_content for d in result.documents]
        if self.embedder:
            vectors = self.embedder.documents(texts)
            if len(vectors) != len(texts):
                raise InputError("embedding count mismatch")
            vectors = [unit_vector(v, self.dimension) for v in vectors]
        else:
            vectors = [None] * len(texts)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            self._check_condition(old, condition)
            if old and (old["source_hash"], old["config_hash"]) == (result.source_hash, result.config_hash):
                return {"status": "unchanged", "revision": self._revision(db), "chunks": len(result.documents),
                        "source_etag": self._source_etag(old), "source_generation": old["generation"], "reader_grants_cleared": 0}
            remaining = db.execute("SELECT count(*),coalesce(sum(length(CAST(normalized_text AS BLOB))),0) FROM sources WHERE name!=?", (source_name,)).fetchone()
            count = db.execute("SELECT count(*) FROM chunks WHERE source!=?", (source_name,)).fetchone()[0]
            if remaining[0] >= 32 or remaining[1] + len(text.encode("utf-8")) > 8*1024*1024 or count+len(texts) > 2000:
                raise InputError("knowledge store source/byte/chunk budget exceeded")
            cleared = db.execute("SELECT count(*) FROM source_grants WHERE source=?", (source_name,)).fetchone()[0]
            db.execute("DELETE FROM sources WHERE name=?", (source_name,))
            generation = self._revision(db) + 1
            db.execute("INSERT INTO sources (name,source_hash,config_hash,normalized_text,generation) VALUES (?,?,?,?,?)",
                       (source_name, result.source_hash, result.config_hash, normalize_text(text), generation))
            for doc, vector in zip(result.documents, vectors):
                m = doc.metadata
                packed = struct.pack(f"<{self.dimension}f", *vector) if vector is not None else None
                db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    m["块ID"], source_name, m["标准编号"], m["标准版本"], m["安全级别"], m["安全类"],
                    m["条款标题"], doc.page_content, canonical_json(m), packed))
            db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            current = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            return {"status": "indexed", "revision": self._revision(db), "chunks": len(texts),
                    "source_etag": self._source_etag(current), "source_generation": generation, "reader_grants_cleared": cleared}

    @staticmethod
    def _revision(db):
        return int(db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0])

    def delete(self, source_name, *, condition=None):
        source_label(source_name)
        condition = require_condition(condition, delete=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            self._check_condition(row, condition)
            count = db.execute("DELETE FROM sources WHERE name=?", (source_name,)).rowcount
            if count:
                db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            return {"status": "deleted" if count else "not_found", "revision": self._revision(db)}

    def _access_snapshot(self, db, row):
        grants = [tuple(g) for g in db.execute("SELECT principal_id,credential_sha256 FROM source_grants WHERE source=? ORDER BY principal_id", (row["name"],))]
        source_etag = self._source_etag(row)
        tag = '"' + sha(canonical_json(["source-access-v1", source_etag, row["access_generation"], grants]).encode("utf-8")) + '"'
        return {"source_name": row["name"], "source_etag": source_etag, "access_etag": tag,
                "access_generation": row["access_generation"], "readers": [g[0] for g in grants]}

    def source_access(self, source_name):
        source_label(source_name)
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            return self._access_snapshot(db, row) if row else None

    def replace_access(self, source_name, readers, *, condition=None):
        source_label(source_name)
        condition = require_condition(condition, delete=True)
        readers = validated_grants(readers)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            current = self._access_snapshot(db, row) if row else None
            if current is None or current["access_etag"] != condition.etag:
                raise WriteRejected("precondition_failed", 412)
            before = [tuple(g) for g in db.execute("SELECT principal_id,credential_sha256 FROM source_grants WHERE source=? ORDER BY principal_id", (source_name,))]
            after = [(g.principal_id, g.credential_sha256) for g in readers]
            if before == after:
                return {"status": "unchanged", **current}
            db.execute("DELETE FROM source_grants WHERE source=?", (source_name,))
            db.executemany("INSERT INTO source_grants VALUES (?,?,?)", [(source_name, *g) for g in after])
            generation = self._revision(db) + 1
            db.execute("UPDATE sources SET access_generation=? WHERE name=?", (generation, source_name))
            db.execute("UPDATE settings SET value=? WHERE key='revision'", (str(generation),))
            row = db.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            return {"status": "updated", **self._access_snapshot(db, row)}

    def publication_valid(self, search, identity=None):
        if identity is not None and not isinstance(identity, ReadIdentity):
            raise ValueError("server-side read identity required")
        with self.connect() as db:
            db.execute("BEGIN")
            if self._revision(db) != search["revision"]:
                return False
            if identity is None or identity.maintainer:
                return True
            sources = {h["metadata"]["来源文件"] for h in search["hits"]}
            return all(db.execute("SELECT 1 FROM source_grants WHERE source=? AND principal_id=? AND credential_sha256=?",
                       (name, identity.principal_id, identity.credential_sha256)).fetchone() is not None for name in sources)

    def search(self, request: SearchRequest, *, identity=None):
        if identity is not None and not isinstance(identity, ReadIdentity):
            raise ValueError("server-side read identity required")
        if request.retrieval != "bm25" and self.embedder is None:
            raise InputError("dense/hybrid retrieval requires an explicitly configured real embedding model")
        scope = request.scope
        with self.connect() as db:
            db.execute("BEGIN")
            revision = self._revision(db)
            sql = "SELECT c.*,s.name,s.generation,s.source_hash,s.config_hash FROM chunks c JOIN sources s ON s.name=c.source WHERE standard_code=? AND edition=? AND level=?"
            params = [scope.standard_code, scope.edition, scope.security_level]
            if scope.security_class is not None:
                sql += " AND security_class=?"
                params.append(scope.security_class)
            if identity is not None and not identity.maintainer:
                sql += " AND EXISTS (SELECT 1 FROM source_grants g WHERE g.source=c.source AND g.principal_id=? AND g.credential_sha256=?)"
                params.extend([identity.principal_id, identity.credential_sha256])
            rows = [dict(row) for row in db.execute(sql + " ORDER BY chunk_id", params)]
            etags = {row["name"]: self._source_etag(row) for row in rows}
        query_vector = unit_vector(self.embedder.query(request.question), self.dimension) if request.retrieval != "bm25" and rows else None
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
            if query_vector is not None:
                row["vector"] = struct.unpack(f"<{self.dimension}f", row["vector"])
        ranked = rank(request.question, rows, request.retrieval, query_vector, self.dense_min_cosine)
        lookup = {row["chunk_id"]: row for row in rows}
        hits, remaining = [], 6000
        for scored in ranked[:request.top_k]:
            row = lookup[scored["chunk_id"]]
            excerpt = row["content"][:min(2000, remaining)]
            if not excerpt:
                continue
            remaining -= len(excerpt)
            hits.append({**scored, "citation_id": f"C{len(hits)+1}", "evidence_text": excerpt,
                         "metadata": row["metadata"], "source_etag": etags[row["source"]],
                         "excerpt_truncated": len(excerpt) < len(row["content"])})
        result = {"revision": revision, "candidate_count": len(rows), "retrieval": request.retrieval,
                  "embedding_identity": self.identity, "dense_min_cosine": self.dense_min_cosine, "hits": hits}
        if not self.publication_valid(result, identity):
            result.update(hits=[], candidate_count=0, invalidated=True)
        return result

    def record_trace(self, trace):
        with self.connect() as db:
            db.execute("INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?)", tuple(trace[k] for k in (
                "trace_id", "revision", "status", "reason", "retrieval", "generation", "candidates", "citations", "total_ms")))
            db.execute("DELETE FROM traces WHERE rowid NOT IN (SELECT rowid FROM traces ORDER BY rowid DESC LIMIT 1000)")

    def trace(self, trace_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            return dict(row) if row else None

    def inventory(self):
        with self.connect() as db:
            db.execute("BEGIN")
            etags = {row["name"]: self._source_etag(row) for row in db.execute("SELECT * FROM sources")}
            rows = db.execute("SELECT source,standard_code,edition,level,security_class,count(*) AS chunks FROM chunks GROUP BY source,standard_code,edition,level,security_class ORDER BY source,level,security_class")
            return {"revision": self._revision(db), "embedding_identity": self.identity,
                    "sources": [{**dict(r), "source_etag": etags[r["source"]]} for r in rows]}
