"""Actual source-authorized RAG on original synthetic files; selector is a test double."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading

from fastapi.testclient import TestClient

from .access import AccessPolicy, AccessSettings
from .api import create_app
from .authorization import ReadIdentity, ReaderGrant
from .conditions import WriteCondition
from .evaluation import sample_config
from .generation import AskRequest, RagService
from .knowledge import KnowledgeStore

DATA = Path(__file__).resolve().parents[1]/"examples/access"
A, B = "teams/a.md", "teams/b.md"


def request_payload():
    return {"question": "进入记录保留多久？", "scope": {"standard_code": "TRAINING-POLICY",
        "edition": "v2", "security_level": "第三级", "security_class": "安全物理环境"}}


def seed(store):
    for name, file in ((A, "team-a.md"), (B, "team-b.md")):
        store.ingest((DATA/file).read_bytes().decode("utf-8"), name, sample_config("v2"), condition=WriteCondition.create())


def semantic_check(embedder):
    """Called by semantic_smoke under forbidden outbound sockets. No selector/HTTP."""
    with tempfile.TemporaryDirectory(prefix="rag-semantic-access-") as directory:
        store = KnowledgeStore(Path(directory)/"dense.sqlite3", embedder)
        seed(store)
        identity = ReadIdentity("synthetic-reader", hashlib.sha256(b"synthetic-scope").hexdigest())
        denied = store.search(AskRequest(**request_payload(), retrieval="dense"), identity=identity)
        assert denied["candidate_count"] == 0 and denied["hits"] == []
        store.replace_access(A, [ReaderGrant(identity.principal_id, identity.credential_sha256)],
                             condition=WriteCondition.match(store.source_access(A)["access_etag"]))
        results = []
        for mode in ("bm25", "dense", "hybrid"):
            result = RagService(store).ask(AskRequest(**request_payload(), retrieval=mode), identity=identity)
            assert result["status"] == "evidence" and result["retrieval"]["candidate_count"] == 1
            assert {c["source"] for c in result["citations"]} == {A}
            assert "乙组" not in json.dumps(result, ensure_ascii=False)
            results.append({"retrieval": mode, "candidates": 1, "citation_sources": [A]})
        return {"default_deny_candidates": 0, "allowed_source_checks": results,
                "scope": "two original synthetic chunks, source authorization check, not a quality benchmark"}


def demonstrate():
    tokens = {name: secrets.token_urlsafe(32) for name in ("alice", "bob", "maintainer")}
    entries = [{"id": name, "role": "maintainer" if name == "maintainer" else "analyst",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for name, token in tokens.items()]
    def headers(name):
        return {"Authorization": "Bearer "+tokens[name]}
    with tempfile.TemporaryDirectory(prefix="rag-source-access-") as directory:
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3")
        seed(store)
        app = create_app(AccessPolicy(AccessSettings(principals=entries)))
        app.state.rag_service = RagService(store)
        with TestClient(app) as client:
            def read_access(name):
                response = client.post("/v1/library/access", json={"source_name": name}, headers=headers("maintainer"))
                assert response.status_code == 200
                return response.json()
            def update(name, readers, tag=None):
                tag = tag or read_access(name)["access_etag"]
                return client.post("/v1/library/access/update", json={"source_name": name, "readers": readers},
                    headers={**headers("maintainer"), "If-Match": tag})
            def ask(name, **extra):
                response = client.post("/v1/ask", json={**request_payload(), **extra}, headers=headers(name))
                assert response.status_code == 200
                return response.json()
            denied = ask("alice")
            assert denied["reason"] == "no_evidence" and denied["retrieval"]["candidate_count"] == 0
            assert update(A, ["alice"]).status_code == update(B, ["bob"]).status_code == 200
            alice, bob = ask("alice"), ask("bob")
            assert "180天" in alice["answer"] and "乙组" not in json.dumps(alice, ensure_ascii=False)
            assert "999天" in bob["answer"] and "甲组" not in json.dumps(bob, ensure_ascii=False)
            old_acl = read_access(A)["access_etag"]
            entered, release = threading.Event(), threading.Event()
            observed = []
            class ControlledSelector:
                def select(self, question, hits):
                    observed.extend(h["metadata"]["来源文件"] for h in hits)
                    entered.set()
                    if not release.wait(15):
                        raise RuntimeError("controlled local selector was not released")
                    return {"abstain": False, "excerpts": [{"citation_id": hits[0]["citation_id"], "quote": hits[0]["evidence_text"]}]}
            app.state.rag_service.generator = ControlledSelector()
            with ThreadPoolExecutor(1) as pool:
                running = pool.submit(ask, "alice", generation="llm")
                try:
                    assert entered.wait(5)
                    assert update(A, []).status_code == 200
                finally:
                    release.set()
                raced = running.result(5)
            assert observed == [A]
            assert raced["reason"] == "knowledge_changed" and raced["answer"] == "" and raced["citations"] == []
            assert not raced["retrieval"]["hits"] and raced["retrieval"]["candidate_count"] == 0
            stale = update(A, ["alice"], old_acl)
            assert stale.status_code == 412
            assert ask("alice")["reason"] == "no_evidence"
            assert update(A, ["alice"]).status_code == 200
            before_replace_acl = read_access(A)["access_etag"]
            source = store.source(A)
            replacement = client.post("/v1/library/ingest", json={"source_name": A,
                "text": (DATA/"team-a.md").read_bytes().decode().replace("180天", "181天"), "config": sample_config("v2").model_dump()},
                headers={**headers("maintainer"), "If-Match": source["source_etag"]})
            assert replacement.status_code == 200 and replacement.json()["reader_grants_cleared"] == 1
            assert ask("alice")["reason"] == "no_evidence"
            assert update(A, ["alice"], before_replace_acl).status_code == 412
            assert update(A, ["alice"]).status_code == 200
            assert "181天" in ask("alice")["answer"]
        alice_entry = entries[0]
        identity = ReadIdentity(alice_entry["id"], alice_entry["token_sha256"])
        assert KnowledgeStore(store.path).search(AskRequest(**request_payload()), identity=identity)["hits"]
        return {"transport": "in-process-asgi", "real_llm": False, "selector": "controlled-local-test-double",
                "documents": 2, "chunks": 2, "same_business_scope": True, "before_grant_candidates": 0,
                "alice_sources": [A], "bob_sources": [B], "selector_sources": observed,
                "revoked_during_selection": {"reason": raced["reason"], "answer": "", "citations": [], "retrieval_hits": [], "candidate_count": 0},
                "stale_access_update": stale.status_code, "replacement_grants_cleared": 1,
                "replacement_before_review": "no_evidence", "replacement_after_review_contains": "181天",
                "grant_persisted_after_reopen": True}


def main():
    print(json.dumps(demonstrate(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
