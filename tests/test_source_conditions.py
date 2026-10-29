from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from compliance_rag.api import create_app
from access_support import HEADERS, make_policy
from compliance_rag.conditions import WriteCondition, WriteRejected
from compliance_rag.evaluation import DATA, sample_config, seed
from compliance_rag.generation import RagService
from compliance_rag.knowledge import KnowledgeStore
from compliance_rag.parser import canonical_json, normalize_text, parse_markdown

NAME = "knowledge/v2.md"
TEXT = (DATA/"v2.md").read_bytes().decode("utf-8")
ROOT = Path(__file__).resolve().parents[1]
app = create_app(make_policy())


@pytest.fixture
def store(tmp_path):
    result = KnowledgeStore(tmp_path/"knowledge.sqlite3")
    seed(result)
    return result


def match(store):
    return WriteCondition.match(store.source(NAME)["source_etag"])


def replace(store, text, condition):
    return store.ingest(text, NAME, sample_config("v2"), condition=condition)


def rejected(status, operation):
    with pytest.raises(WriteRejected) as error:
        operation()
    assert error.value.status == status


def test_create_only_never_overwrites_even_same_content(store):
    before = store.source(NAME)
    rejected(412, lambda: replace(store, TEXT, WriteCondition.create()))
    assert store.source(NAME) == before


def test_both_mutations_require_preconditions(store):
    before = store.source(NAME)
    rejected(428, lambda: replace(store, TEXT, None))
    rejected(428, lambda: store.delete(NAME))
    assert store.source(NAME) == before


def test_changed_source_rejects_stale_edit(store):
    old = match(store)
    new = replace(store, TEXT.replace("180天", "181天"), old)
    rejected(412, lambda: replace(store, TEXT.replace("180天", "182天"), old))
    assert store.source(NAME)["source_etag"] == new["source_etag"]
    assert "181天" in store.source(NAME)["normalized_text"]


def test_stale_delete_preserves_new_knowledge(store):
    old = match(store)
    replace(store, TEXT.replace("180天", "181天"), old)
    rejected(412, lambda: store.delete(NAME, condition=old))
    assert store.source(NAME) is not None


def test_unchanged_write_keeps_etag_and_generation(store):
    old = store.source(NAME)
    revision = store.revision()
    result = replace(store, TEXT, match(store))
    assert result["status"] == "unchanged"
    assert result["source_etag"] == old["source_etag"]
    assert result["source_generation"] == old["source_generation"]
    assert result["revision"] == revision


def test_content_aba_does_not_resurrect_old_tag(store):
    old = match(store)
    replace(store, TEXT.replace("180天", "181天"), old)
    replace(store, TEXT, match(store))
    assert store.source(NAME)["normalized_text"] == normalize_text(TEXT)
    assert match(store) != old
    rejected(412, lambda: replace(store, TEXT.replace("180天", "182天"), old))


def test_delete_recreate_identical_source_does_not_resurrect_tag(store):
    old = match(store)
    store.delete(NAME, condition=old)
    replace(store, TEXT, WriteCondition.create())
    assert match(store) != old
    rejected(412, lambda: store.delete(NAME, condition=old))


def test_unrelated_edit_does_not_expire_source_etag(store):
    old = match(store)
    other = "knowledge/v1.md"
    store.ingest((DATA/"v1.md").read_bytes().decode().replace("30天", "31天"), other,
                 sample_config("v1"), condition=WriteCondition.match(store.source(other)["source_etag"]))
    assert match(store) == old
    assert replace(store, TEXT.replace("180天", "181天"), old)["status"] == "indexed"


def test_same_text_different_logical_source_cannot_share_precondition(store):
    result = store.ingest(TEXT,"copy.md",sample_config("v2"),condition=WriteCondition.create())
    rejected(412, lambda: store.delete(NAME,condition=WriteCondition.match(result["source_etag"])))
    assert store.source(NAME) is not None


def test_config_only_change_renews_tag(store):
    old = store.source(NAME)
    config = sample_config("v2").model_copy(update={"max_chars":256})
    changed = store.ingest(TEXT,NAME,config,condition=WriteCondition.match(old["source_etag"]))
    assert changed["status"] == "indexed" and changed["source_etag"] != old["source_etag"]
    assert store.source(NAME)["source_sha256"] == old["source_sha256"]
    assert store.source(NAME)["config_sha256"] != old["config_sha256"]


def test_reopen_keeps_tag_but_independent_database_changes_it(store,tmp_path):
    assert KnowledgeStore(store.path).source(NAME)["source_etag"] == store.source(NAME)["source_etag"]
    other = KnowledgeStore(tmp_path/"independent.sqlite3")
    seed(other)
    assert other.source(NAME)["source_etag"] != store.source(NAME)["source_etag"]


def test_unknown_source_does_not_match_arbitrary_tag(store):
    condition = WriteCondition.match('"' + '0'*64 + '"')
    rejected(412, lambda: store.delete("unknown.md", condition=condition))
    assert store.source("unknown.md") is None


class ControlledEmbedder:
    identity, dimension = "TEST-ONLY-NOT-REAL", 2
    def documents(self,texts):
        return [[1,1] for _ in texts]
    def query(self,text):
        return [1,1]


def test_stale_submission_rejected_before_embedding(tmp_path):
    embedder = ControlledEmbedder()
    store = KnowledgeStore(tmp_path/"dense.sqlite3",embedder)
    seed(store)
    old = match(store)
    replace(store, TEXT.replace("180天", "181天"),old)
    embedder.documents = lambda _: (_ for _ in ()).throw(AssertionError("stale data reached embedder"))
    rejected(412, lambda: replace(store,TEXT.replace("180天", "182天"),old))


def test_competing_commit_during_embedding_is_rechecked(tmp_path):
    embedder = ControlledEmbedder()
    store = KnowledgeStore(tmp_path/"dense.sqlite3",embedder)
    seed(store)
    competitor = KnowledgeStore(store.path,ControlledEmbedder())
    old = match(store)
    def embedding(texts):
        replace(competitor,TEXT.replace("180天", "182天"),old)
        return [[1,1] for _ in texts]
    embedder.documents = embedding
    rejected(412, lambda: replace(store,TEXT.replace("180天", "181天"),old))
    assert "182天" in store.source(NAME)["normalized_text"]
    assert store.revision() == 3


def test_concurrent_create_only_one_winner(tmp_path):
    path = tmp_path/"shared.sqlite3"
    KnowledgeStore(path)
    def create(_):
        try:
            return replace(KnowledgeStore(path),TEXT,WriteCondition.create())["status"]
        except WriteRejected as error:
            assert error.status == 412
            return "conflict"
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create, range(8)))
    assert outcomes.count("indexed") == 1 and outcomes.count("conflict") == 7
    assert KnowledgeStore(path).revision() == 1


def test_four_real_cli_processes_share_one_commit_guard(store,tmp_path):
    source, config = tmp_path/"source.md", tmp_path/"config.json"
    source.write_text(TEXT.replace("180天","181天"),encoding="utf-8")
    config.write_text(sample_config("v2").model_dump_json(),encoding="utf-8")
    old = store.source(NAME)["source_etag"].strip('"')
    command = [sys.executable,"-X","utf8","-m","compliance_rag.rag_cli","ingest",str(source),
        "--db",str(store.path),"--config",str(config),"--source-name",NAME,"--if-match",old]
    processes = [subprocess.Popen(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(4)]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        statuses = [process.returncode for process in processes]
        assert sorted(statuses) == [0,3,3,3], [part.decode("utf-8",errors="replace") for out in outputs for part in out]
        assert store.revision() == 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()  # Only the exact subprocess created in this test.
                process.wait(timeout=5)


def make_legacy(path):
    """An independent schema-1 fixture, not a schema-2 database relabeled as old."""
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE sources(name TEXT PRIMARY KEY,source_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,normalized_text TEXT NOT NULL);
          CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,source TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
            standard_code TEXT NOT NULL,edition TEXT NOT NULL,level TEXT NOT NULL,security_class TEXT NOT NULL,
            title TEXT NOT NULL,content TEXT NOT NULL,metadata TEXT NOT NULL,vector BLOB);
          CREATE TABLE traces(trace_id TEXT PRIMARY KEY,revision INTEGER,status TEXT,reason TEXT,
            retrieval TEXT,generation TEXT,candidates INTEGER,citations INTEGER,total_ms REAL);
        """)
        db.executemany("INSERT INTO settings VALUES (?,?)",[("revision","2"),("schema","1"),("embedding","none"),("dimension","0")])
        for edition in ("v1","v2"):
            text = (DATA/f"{edition}.md").read_bytes().decode("utf-8")
            name = f"knowledge/{edition}.md"
            parsed = parse_markdown(text,name,sample_config(edition))
            db.execute("INSERT INTO sources VALUES (?,?,?,?)",(name,parsed.source_hash,parsed.config_hash,normalize_text(text)))
            for document in parsed.documents:
                m = document.metadata
                db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",(m["块ID"],name,m["标准编号"],
                    m["标准版本"],m["安全级别"],m["安全类"],m["条款标题"],document.page_content,canonical_json(m),None))


def test_schema_one_migration_preserves_rows_revision_and_text(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    make_legacy(path)
    with sqlite3.connect(path) as db:
        before = list(db.execute("SELECT * FROM chunks ORDER BY chunk_id"))
    store = KnowledgeStore(path)
    assert store.revision() == 2
    assert store.source(NAME)["normalized_text"] == normalize_text(TEXT)
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM chunks ORDER BY chunk_id")] == before
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "3"
    assert KnowledgeStore(path).source(NAME)["source_etag"] == store.source(NAME)["source_etag"]


def test_migration_fault_rolls_back_column_and_settings(tmp_path,monkeypatch):
    path = tmp_path/"legacy.sqlite3"
    make_legacy(path)
    with monkeypatch.context() as context:
        context.setattr(KnowledgeStore,"_revision",staticmethod(lambda db: (_ for _ in ()).throw(RuntimeError("injected migration failure"))))
        with pytest.raises(RuntimeError):
            KnowledgeStore(path)
    with sqlite3.connect(path) as db:
        assert "generation" not in {row[1] for row in db.execute("PRAGMA table_info(sources)")}
        assert db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] == "1"
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 12
    assert KnowledgeStore(path).source(NAME)


def test_concurrent_legacy_open_migrates_once(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    make_legacy(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tags = list(pool.map(lambda _: KnowledgeStore(path).source(NAME)["source_etag"],range(8)))
    assert len(set(tags)) == 1


def test_model_mismatch_does_not_migrate_legacy(tmp_path):
    path = tmp_path/"legacy.sqlite3"
    make_legacy(path)
    from compliance_rag.parser import InputError
    with pytest.raises(InputError):
        KnowledgeStore(path,ControlledEmbedder())
    with sqlite3.connect(path) as db:
        assert "generation" not in {row[1] for row in db.execute("PRAGMA table_info(sources)")}


@pytest.fixture
def client(store):
    previous = app.state.rag_service
    app.state.rag_service = RagService(store)
    try:
        with TestClient(app, headers=HEADERS) as client:
            yield client
    finally:
        app.state.rag_service = previous


def payload(text=TEXT):
    return {"text":text,"source_name":NAME,"config":sample_config("v2").model_dump()}


def test_http_read_update_stale_update_and_missing_condition(client):
    response = client.post("/v1/library/source",json={"source_name":NAME})
    assert response.status_code == 200 and response.headers["etag"] == response.json()["source_etag"]
    old = response.headers["etag"]
    assert client.post("/v1/library/ingest",json=payload()).status_code == 428
    fresh = client.post("/v1/library/ingest",json=payload(TEXT.replace("180天","181天")),headers={"If-Match":old})
    assert fresh.status_code == 200 and fresh.headers["etag"] != old
    failed = client.post("/v1/library/ingest",json=payload(TEXT.replace("180天","182天")),headers={"If-Match":old})
    assert failed.status_code == 412
    assert "etag" not in failed.headers
    assert fresh.headers["etag"] not in failed.text
    assert failed.headers["cache-control"] == "no-store"


def test_http_delete_old_tag_is_not_accepted(client):
    old = client.post("/v1/library/source",json={"source_name":NAME}).headers["etag"]
    updated = client.post("/v1/library/ingest",json=payload(TEXT.replace("180天","181天")),headers={"If-Match":old})
    assert client.post("/v1/library/remove",json={"source_name":NAME},headers={"If-Match":old}).status_code == 412
    assert client.post("/v1/library/remove",json={"source_name":NAME},headers={"If-Match":updated.headers["etag"]}).status_code == 200
    assert client.post("/v1/library/source",json={"source_name":NAME}).status_code == 404


def test_strong_source_representation_does_not_include_unrelated_global_revision(client,store):
    before = client.post("/v1/library/source",json={"source_name":NAME})
    other = "knowledge/v1.md"
    store.delete(other,condition=WriteCondition.match(store.source(other)["source_etag"]))
    after = client.post("/v1/library/source",json={"source_name":NAME})
    assert after.headers["etag"] == before.headers["etag"]
    assert after.content == before.content  # No unrelated collection revision inside this representation.


def test_http_missing_precondition_response_is_not_cacheable(client):
    response = client.post("/v1/library/ingest",json=payload())
    assert response.status_code == 428 and response.headers["cache-control"] == "no-store"


def test_http_create_and_repeat_never_silently_replace(client):
    request = {**payload(),"source_name":"new-source.md"}
    first = client.post("/v1/library/ingest",json=request,headers={"If-None-Match":"*"})
    assert first.status_code == 200
    assert client.post("/v1/library/ingest",json=request,headers={"If-None-Match":"*"}).status_code == 412


def test_reference_demo_is_actual_conditional_store_behavior():
    from compliance_rag.conditional_demo import run
    result = run()
    assert result == json.loads((ROOT/"examples/evaluation/conditional-writes.json").read_text(encoding="utf-8"))
    assert result["threaded_writers"] == {"attempts":8,"committed":1,"conflicts":7,"revision_increments":1}
    assert result["content_aba_old_tag"] == result["delete_recreate_old_tag"] == 412


@pytest.mark.parametrize("headers", [
    {"If-Match":"*"}, {"If-Match":'W/"'+'0'*64+'"'}, {"If-Match":"0"*64},
    {"If-Match":'"'+'0'*64+'", "'+'1'*64+'"'},
    {"If-None-Match":'"'+'0'*64+'"'}, {"If-Match":'"'+'0'*64+'"',"If-None-Match":"*"},
    [("If-Match",'"'+'0'*64+'"'),("If-Match",'"'+'0'*64+'"')],
    [("If-None-Match","*"),("If-None-Match","*")],
])
def test_ambiguous_or_unsupported_conditions_fail_closed(client,headers):
    response = client.post("/v1/library/ingest",json=payload(),headers=headers)
    assert response.status_code == 400


def test_create_only_header_not_accepted_for_delete(client):
    assert client.post("/v1/library/remove",json={"source_name":NAME},headers={"If-None-Match":"*"}).status_code == 400


def test_citations_and_inventory_bind_the_read_source_etag(store):
    from compliance_rag.generation import AskRequest
    response = RagService(store).ask(AskRequest(question="进入记录保留多久",scope={
        "standard_code":"TRAINING-POLICY","edition":"v2","security_level":"第三级","security_class":"安全物理环境"}))
    assert response["citations"][0]["source_etag"] == store.source(NAME)["source_etag"]
    assert all(row["source_etag"] == store.source(row["source"])["source_etag"] for row in store.inventory()["sources"])
