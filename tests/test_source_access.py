from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from compliance_rag import knowledge
from compliance_rag.access import AccessPolicy, AccessSettings
from compliance_rag.api import create_app
from compliance_rag.authorization import ReadIdentity, ReaderGrant
from compliance_rag.conditions import WriteCondition, WriteRejected
from compliance_rag.evaluation import DATA, sample_config
from compliance_rag.generation import AskRequest, RagService
from compliance_rag.knowledge import KnowledgeStore
from compliance_rag.parser import canonical_json, normalize_text, parse_markdown, sha

ROOT = Path(__file__).resolve().parents[1]
A, B = "teams/a.md", "teams/b.md"
TEXT_A = (ROOT/"examples/access/team-a.md").read_text(encoding="utf-8")
TEXT_B = (ROOT/"examples/access/team-b.md").read_text(encoding="utf-8")
ALICE = ReadIdentity("alice", hashlib.sha256(b"synthetic-alice").hexdigest())
BOB = ReadIdentity("bob", hashlib.sha256(b"synthetic-bob").hexdigest())
ADMIN = ReadIdentity("maintainer", hashlib.sha256(b"synthetic-admin").hexdigest(), True)
TOKENS = {name: "synthetic_"+name+"_"+"c"*43 for name in ("alice", "bob", "maintainer")}


class ProbeEmbedder:
    """Deterministic unit-test double, not semantic model evidence."""
    identity, dimension = "source-access-test-double", 2
    def __init__(self):
        self.queries = 0
        self.callback = None
    def documents(self, texts):
        return [[1, 1] for _ in texts]
    def query(self, question):
        self.queries += 1
        if self.callback:
            self.callback()
        return [1, 1]


def question(mode="bm25", generation="extractive"):
    return AskRequest(question="进入记录保留多久？", scope={"standard_code": "TRAINING-POLICY",
        "edition": "v2", "security_level": "第三级", "security_class": "安全物理环境"},
        retrieval=mode, generation=generation)


def acl_condition(store, name=A):
    return WriteCondition.match(store.source_access(name)["access_etag"])


def grant(store, name, *identities):
    return store.replace_access(name, [ReaderGrant(i.principal_id, i.credential_sha256) for i in identities],
                                condition=acl_condition(store, name))


def seeded(path, embedder=None):
    store = KnowledgeStore(path, embedder)
    for name, text in ((A, TEXT_A), (B, TEXT_B)):
        store.ingest(text, name, sample_config("v2"), condition=WriteCondition.create())
    return store


@pytest.fixture
def store(tmp_path):
    return seeded(tmp_path/"knowledge.sqlite3")


@pytest.fixture
def client(store):
    entries = [{"id": name, "role": "maintainer" if name == "maintainer" else "analyst",
        "token_sha256": hashlib.sha256(value.encode()).hexdigest()} for name, value in TOKENS.items()]
    app = create_app(AccessPolicy(AccessSettings(principals=entries)))
    app.state.rag_service = RagService(store)
    with TestClient(app) as client:
        yield client


def headers(name="maintainer"):
    return {"Authorization": "Bearer "+TOKENS[name]}


def api_grant(client, source=A, readers=None, tag=None):
    if tag is None:
        tag = client.post("/v1/library/access", json={"source_name": source}, headers=headers()).headers["etag"]
    return client.post("/v1/library/access/update", json={"source_name": source, "readers": readers or []},
                       headers={**headers(), "If-Match": tag})


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid"])
def test_default_deny_before_ranking_or_query_embedding(tmp_path, monkeypatch, mode):
    embedder = ProbeEmbedder()
    store = seeded(tmp_path/"dense.sqlite3", embedder)
    original, observed = knowledge.rank, []
    def inspect(*args):
        observed.extend(args[1])
        return original(*args)
    monkeypatch.setattr(knowledge, "rank", inspect)
    result = store.search(question(mode), identity=ALICE)
    assert result["hits"] == [] and result["candidate_count"] == 0
    assert not observed and embedder.queries == 0


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid"])
def test_acl_before_scoring_context_and_statistics(tmp_path, monkeypatch, mode):
    store = seeded(tmp_path/"two.sqlite3", ProbeEmbedder())
    grant(store, A, ALICE)
    grant(store, B, BOB)
    reference = KnowledgeStore(tmp_path/"one.sqlite3", ProbeEmbedder())
    reference.ingest(TEXT_A, A, sample_config("v2"), condition=WriteCondition.create())
    original, observed = knowledge.rank, []
    def inspect(query, rows, *args):
        observed.extend(rows)
        return original(query, rows, *args)
    monkeypatch.setattr(knowledge, "rank", inspect)
    result = store.search(question(mode), identity=ALICE)
    expected = reference.search(question(mode))
    assert result["candidate_count"] == 1 and len(result["hits"]) == 1
    assert all(row["source"] == A for row in observed)
    # Different databases deliberately have different source ETags; scores,
    # evidence and provenance must otherwise equal the one-source corpus.
    assert result["hits"][0]["source_etag"] != expected["hits"][0]["source_etag"]
    assert [{k: v for k, v in h.items() if k != "source_etag"} for h in result["hits"]] == [
        {k: v for k, v in h.items() if k != "source_etag"} for h in expected["hits"]]
    assert "999天" not in json.dumps(result, ensure_ascii=False)
    assert "乙组" not in json.dumps(result, ensure_ascii=False)


def test_principal_isolation_maintainer_and_trusted_local_bypass(store):
    grant(store, A, ALICE)
    grant(store, B, BOB)
    assert {h["metadata"]["来源文件"] for h in store.search(question(), identity=BOB)["hits"]} == {B}
    assert store.search(question(), identity=ALICE)["candidate_count"] == 1
    assert store.search(question(), identity=ADMIN)["candidate_count"] == 2
    assert store.search(question())["candidate_count"] == 2


def test_grants_persist_across_reopen_and_do_not_change_content_etag(store):
    before = store.source(A)
    grant(store, A, ALICE)
    reopened = KnowledgeStore(store.path)
    assert reopened.search(question(), identity=ALICE)["hits"]
    assert reopened.source(A) == before


def test_reusing_id_with_a_new_credential_does_not_inherit_permission(store):
    grant(store, A, ALICE)
    rotated = ReadIdentity("alice", hashlib.sha256(b"synthetic-rotated").hexdigest())
    old = store.source_access(A)
    assert store.search(question(), identity=rotated)["hits"] == []
    updated = grant(store, A, rotated)
    assert updated["readers"] == old["readers"] == ["alice"]
    assert updated["access_etag"] != old["access_etag"]
    assert store.search(question(), identity=rotated)["hits"]
    assert not store.search(question(), identity=ALICE)["hits"]


def test_real_replace_resets_grants_but_noop_preserves_them(store):
    grant(store, A, ALICE)
    old_acl = store.source_access(A)
    condition = WriteCondition.match(store.source(A)["source_etag"])
    unchanged = store.ingest(TEXT_A, A, sample_config("v2"), condition=condition)
    assert unchanged["reader_grants_cleared"] == 0 and store.source_access(A) == old_acl
    replaced = store.ingest(TEXT_A.replace("180天", "181天"), A, sample_config("v2"), condition=condition)
    assert replaced["reader_grants_cleared"] == 1
    assert store.source_access(A)["readers"] == []
    assert store.search(question(), identity=ALICE)["candidate_count"] == 0
    with pytest.raises(WriteRejected):
        store.replace_access(A, [ReaderGrant(ALICE.principal_id, ALICE.credential_sha256)],
                             condition=WriteCondition.match(old_acl["access_etag"]))


def test_config_change_resets_grants(store):
    grant(store, A, ALICE)
    config = sample_config("v2").model_copy(update={"max_chars": 400})
    result = store.ingest(TEXT_A, A, config, condition=WriteCondition.match(store.source(A)["source_etag"]))
    assert result["reader_grants_cleared"] == 1 and not store.source_access(A)["readers"]


def test_deletion_recreation_cascades_grants_and_expires_acl_tag(store):
    grant(store, A, ALICE)
    old = acl_condition(store)
    store.delete(A, condition=WriteCondition.match(store.source(A)["source_etag"]))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM source_grants WHERE source=?", (A,)).fetchone()[0] == 0
    store.ingest(TEXT_A, A, sample_config("v2"), condition=WriteCondition.create())
    assert store.source_access(A)["readers"] == []
    with pytest.raises(WriteRejected):
        store.replace_access(A, [], condition=old)


def test_acl_etag_noop_unrelated_edit_and_aba(store):
    old = store.source_access(A)
    revision = store.revision()
    assert store.replace_access(A, [], condition=acl_condition(store))["status"] == "unchanged"
    assert store.revision() == revision
    grant(store, B, BOB)
    assert store.source_access(A) == old
    grant(store, A, ALICE)
    grant(store, A)
    assert store.source_access(A)["readers"] == old["readers"] == []
    assert store.source_access(A)["access_etag"] != old["access_etag"]
    with pytest.raises(WriteRejected):
        store.replace_access(A, [], condition=WriteCondition.match(old["access_etag"]))


@pytest.mark.parametrize("kind", ["missing", "source-tag", "other-source", "create"])
def test_acl_write_requires_its_own_current_condition(store, kind):
    conditions = {"missing": None, "source-tag": WriteCondition.match(store.source(A)["source_etag"]),
                  "other-source": acl_condition(store, B), "create": WriteCondition.create()}
    before = store.revision()
    with pytest.raises(WriteRejected):
        store.replace_access(A, [], condition=conditions[kind])
    assert store.revision() == before and store.source_access(A)["readers"] == []


def test_concurrent_acl_writers_one_winner_across_connections(store):
    old = acl_condition(store)
    before = store.revision()
    def attempt(i):
        second = KnowledgeStore(store.path)
        try:
            second.replace_access(A, [ReaderGrant(f"reviewer{i}", sha(f"synthetic-{i}".encode()))], condition=old)
            return 200
        except WriteRejected as error:
            return error.status
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    assert statuses.count(200) == 1 and statuses.count(412) == 7
    assert store.revision() == before+1


def test_acl_mutation_failure_rolls_back_readers_revision_and_etag(store):
    grant(store, A, ALICE)
    before, revision = store.source_access(A), store.revision()
    with store.connect() as db:
        db.execute("CREATE TRIGGER deny_grants BEFORE INSERT ON source_grants BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        grant(store, A, BOB)
    assert store.source_access(A) == before and store.revision() == revision
    assert store.search(question(), identity=ALICE)["hits"]


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
def test_revoke_in_query_embedding_discards_search_snapshot(tmp_path, mode):
    embedder = ProbeEmbedder()
    store = seeded(tmp_path/"race.sqlite3", embedder)
    grant(store, A, ALICE)
    competitor = KnowledgeStore(store.path, ProbeEmbedder())
    embedder.callback = lambda: grant(competitor, A)
    result = store.search(question(mode), identity=ALICE)
    assert result["invalidated"] is True and not result["hits"] and result["candidate_count"] == 0


def test_revoke_between_search_and_generation_never_calls_selector(store, monkeypatch):
    grant(store, A, ALICE)
    original = store.search
    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        grant(store, A)
        return result
    monkeypatch.setattr(store, "search", raced)
    class ForbiddenSelector:
        def select(self, *_):
            pytest.fail("revoked snapshot was sent to selector")
    result = RagService(store, ForbiddenSelector()).ask(question(generation="llm"), identity=ALICE)
    assert result["reason"] == "knowledge_changed" and not result["retrieval"]["hits"]


def test_only_authorized_evidence_reaches_selector_and_final_citations(store):
    grant(store, A, ALICE)
    grant(store, B, BOB)
    seen = []
    class Selecting:
        def select(self, question, hits):
            seen.extend(hits)
            return {"abstain": False, "excerpts": [{"citation_id": hits[0]["citation_id"], "quote": hits[0]["evidence_text"]}]}
    result = RagService(store, Selecting()).ask(question(generation="llm"), identity=ALICE)
    assert result["status"] == "evidence" and len(seen) == 1
    assert "乙组" not in json.dumps(result, ensure_ascii=False)
    assert result["citations"][0]["source"] == A


def test_revoke_while_selector_runs_discards_answer_and_all_evidence(store):
    grant(store, A, ALICE)
    class Revoking:
        def select(self, question, hits):
            grant(KnowledgeStore(store.path), A)
            return {"abstain": False, "excerpts": [{"citation_id": hits[0]["citation_id"], "quote": hits[0]["evidence_text"]}]}
    result = RagService(store, Revoking()).ask(question(generation="llm"), identity=ALICE)
    assert result["status"] == "refused" and result["reason"] == "knowledge_changed"
    assert result["answer"] == "" and result["citations"] == []
    assert not result["retrieval"]["hits"] and result["retrieval"]["candidate_count"] == 0


def test_real_http_handlers_grant_revoke_and_filter(client):
    payload = question().model_dump()
    before = client.post("/v1/ask", json=payload, headers=headers("alice")).json()
    assert before["reason"] == "no_evidence" and before["retrieval"]["candidate_count"] == 0
    assert api_grant(client, A, ["alice"]).status_code == 200
    assert api_grant(client, B, ["bob"]).status_code == 200
    visible = client.post("/v1/ask", json=payload, headers=headers("alice"))
    assert visible.status_code == 200 and "180天" in visible.json()["answer"]
    assert "乙组" not in visible.text and B not in visible.text
    assert api_grant(client, A, []).status_code == 200
    assert client.post("/v1/ask", json=payload, headers=headers("alice")).json()["reason"] == "no_evidence"


@pytest.mark.parametrize("name", ["alice", "bob"])
@pytest.mark.parametrize("route", ["/v1/library/access", "/v1/library/access/update"])
def test_query_roles_cannot_enumerate_or_self_grant(client, name, route):
    assert client.post(route, json={"source_name": A, "readers": [name]}, headers=headers(name)).status_code == 403


@pytest.mark.parametrize("readers", [["unconfigured"], ["maintainer"], ["alice", "alice"], ["../alice"], [True], "alice", ["x"]*17])
def test_acl_api_rejects_invalid_or_unconfigured_readers_without_changes(client, store, readers):
    before = store.source_access(A)
    response = client.post("/v1/library/access/update", json={"source_name": A, "readers": readers},
                           headers={**headers(), "If-Match": before["access_etag"]})
    assert response.status_code == 422 and store.source_access(A) == before


@pytest.mark.parametrize("field", ["identity", "principal_id", "credential_sha256", "maintainer", "allowed_sources"])
def test_client_cannot_supply_read_authority(client, field):
    response = client.post("/v1/ask", json={**question().model_dump(), field: "spoofed"}, headers=headers("alice"))
    assert response.status_code == 422


def test_access_snapshot_never_exposes_credential_fingerprint(client):
    assert api_grant(client, A, ["alice"]).status_code == 200
    response = client.post("/v1/library/access", json={"source_name": A}, headers=headers())
    assert response.headers["etag"] == response.json()["access_etag"]
    assert response.headers["cache-control"] == "no-store"
    assert hashlib.sha256(TOKENS["alice"].encode()).hexdigest() not in response.text
    assert response.json()["readers"] == ["alice"]


def make_schema_two(path):
    """Independent old layout with real parsed synthetic chunks, not relabeled v3."""
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE sources(name TEXT PRIMARY KEY,source_hash TEXT NOT NULL,config_hash TEXT NOT NULL,
            normalized_text TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
            standard_code TEXT NOT NULL,edition TEXT NOT NULL,level TEXT NOT NULL,security_class TEXT NOT NULL,
            title TEXT NOT NULL,content TEXT NOT NULL,metadata TEXT NOT NULL,vector BLOB);
        """)
        db.executemany("INSERT INTO settings VALUES (?,?)", [("schema","2"),("revision","2"),
            ("embedding","none"),("dimension","0"),("instance_id","f"*32)])
        for edition in ("v1", "v2"):
            name, text = f"knowledge/{edition}.md", (DATA/f"{edition}.md").read_bytes().decode()
            parsed = parse_markdown(text, name, sample_config(edition))
            db.execute("INSERT INTO sources VALUES (?,?,?,?,2)", (name, parsed.source_hash, parsed.config_hash, normalize_text(text)))
            for doc in parsed.documents:
                m = doc.metadata
                db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", (m["块ID"], name, m["标准编号"],
                    m["标准版本"], m["安全级别"], m["安全类"], m["条款标题"], doc.page_content, canonical_json(m), None))


def test_schema_two_upgrade_preserves_chunks_content_etags_and_denies_readers(tmp_path):
    path = tmp_path/"old.sqlite3"
    make_schema_two(path)
    with sqlite3.connect(path) as db:
        chunks = list(db.execute("SELECT * FROM chunks ORDER BY chunk_id"))
        sources = list(db.execute("SELECT * FROM sources ORDER BY name"))
    old_tags = {r[0]: '"'+sha(canonical_json(["f"*32, r[0], r[4], r[1], r[2]]).encode())+'"' for r in sources}
    store = KnowledgeStore(path)
    assert store.revision() == 2
    assert {name: store.source(name)["source_etag"] for name in old_tags} == old_tags
    assert store.search(question(), identity=ALICE)["hits"] == []
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM chunks ORDER BY chunk_id")] == chunks
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 12
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "3"


def test_schema_two_migration_failure_is_atomic(tmp_path, monkeypatch):
    path = tmp_path/"old.sqlite3"
    make_schema_two(path)
    original = KnowledgeStore._migrate_source_access
    def fail(db):
        original(db)
        raise RuntimeError("injected after new ACL schema")
    with monkeypatch.context() as context:
        context.setattr(KnowledgeStore, "_migrate_source_access", staticmethod(fail))
        with pytest.raises(RuntimeError):
            KnowledgeStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "2"
        assert "access_generation" not in {r[1] for r in db.execute("PRAGMA table_info(sources)")}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='source_grants'").fetchone() is None
    assert KnowledgeStore(path).source("knowledge/v2.md")


def test_schema_two_concurrent_upgrade_runs_once(tmp_path):
    path = tmp_path/"old.sqlite3"
    make_schema_two(path)
    with ThreadPoolExecutor(4) as pool:
        snapshots = list(pool.map(lambda _: KnowledgeStore(path).source_access("knowledge/v2.md"), range(8)))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert snapshots[0]["readers"] == []


def test_demo_matches_public_fixed_evidence():
    from compliance_rag.source_access_demo import demonstrate
    expected = json.loads((ROOT/"examples/evaluation/source-access.json").read_text(encoding="utf-8"))
    assert demonstrate() == expected


def test_api_search_suppresses_stale_dense_results(client, tmp_path):
    embedder = ProbeEmbedder()
    dense = seeded(tmp_path/"dense-api.sqlite3", embedder)
    client.app.state.rag_service = RagService(dense)
    assert api_grant(client, A, ["alice"]).status_code == 200
    competitor = KnowledgeStore(dense.path, ProbeEmbedder())
    embedder.callback = lambda: grant(competitor, A)
    response = client.post("/v1/search", json=question("dense").model_dump(exclude={"generation"}), headers=headers("alice"))
    assert response.status_code == 409
    assert response.json() == {"error": "knowledge_or_access_changed", "hits": [], "candidate_count": 0}
    assert "180天" not in response.text and A not in response.text


def test_schema_two_model_mismatch_does_not_migrate(tmp_path):
    from compliance_rag.parser import InputError
    path = tmp_path/"old.sqlite3"
    make_schema_two(path)
    with pytest.raises(InputError):
        KnowledgeStore(path, ProbeEmbedder())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "2"
        assert "access_generation" not in {r[1] for r in db.execute("PRAGMA table_info(sources)")}


@pytest.mark.parametrize("readers", [None, [ALICE], [ReaderGrant("alice", "f"*64)]*2,
    [ReaderGrant(f"id{i}", "f"*64) for i in range(17)]])
def test_local_api_rejects_invalid_grant_collection_without_changes(store, readers):
    before = store.source_access(A)
    with pytest.raises(ValueError):
        store.replace_access(A, readers, condition=acl_condition(store))
    assert store.source_access(A) == before


def test_source_access_missing_resource_and_cas_error_contract(client):
    assert client.post("/v1/library/access", json={"source_name": "missing.md"}, headers=headers()).status_code == 404
    payload = {"source_name": A, "readers": ["alice"]}
    assert client.post("/v1/library/access/update", json=payload, headers=headers()).status_code == 428
    for invalid in ('W/"'+'f'*64+'"', '"'+'f'*64+'", "'+'a'*64+'"'):
        assert client.post("/v1/library/access/update", json=payload, headers={**headers(), "If-Match": invalid}).status_code == 400
