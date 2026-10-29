"""Synthetic RAG authorization/admission evidence; ASGI client, no external model."""
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
from .evaluation import seed
from .generation import RagService
from .knowledge import KnowledgeStore
from .authorization import ReaderGrant
from .conditions import WriteCondition


def demonstrate():
    tokens = {role: secrets.token_urlsafe(32) for role in ("reader", "analyst", "maintainer")}
    entries = [{"id": role, "role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
               for role, token in tokens.items()]
    def headers(role):
        return {"Authorization": "Bearer " + tokens[role]}
    payload = {"question": "进入记录保留多久？", "scope": {
        "standard_code": "TRAINING-POLICY", "edition": "v2", "security_level": "第三级"}}
    with tempfile.TemporaryDirectory(prefix="rag-access-") as directory:
        app = create_app(AccessPolicy(AccessSettings(principals=entries)))
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3")
        seed(store)
        store.replace_access("knowledge/v2.md", [ReaderGrant(e["id"], e["token_sha256"]) for e in entries if e["role"] != "maintainer"],
                             condition=WriteCondition.match(store.source_access("knowledge/v2.md")["access_etag"]))
        revision = store.revision()
        app.state.rag_service = RagService(store)
        with TestClient(app) as client:
            auth = [client.get("/v1/library").status_code,
                    client.post("/v1/library/remove", json={"source_name": "knowledge/v2.md"},
                                headers=headers("reader")).status_code,
                    client.post("/v1/ask", json=payload, headers=headers("reader")).status_code,
                    client.post("/v1/ask", json={**payload, "generation": "llm"}, headers=headers("reader")).status_code]
            assert auth == [401, 403, 200, 403] and store.revision() == revision
            entered, release = threading.Event(), threading.Event()
            class ControlledSelector:
                calls = 0
                def select(self, question, hits):
                    self.calls += 1
                    entered.set()
                    if not release.wait(15):
                        raise RuntimeError("controlled local worker was not released")
                    return {"abstain": True, "excerpts": []}
            selector = ControlledSelector()
            app.state.rag_service.generator = selector
            with ThreadPoolExecutor(8) as pool:
                first = pool.submit(client.post, "/v1/ask", json={**payload, "generation": "llm"}, headers=headers("analyst"))
                try:
                    assert entered.wait(5)
                    futures = [pool.submit(client.post, "/v1/ask", json=payload, headers=headers("analyst")) for _ in range(7)]
                    conflicts = [future.result(5).status_code for future in futures]
                    llm_busy = client.post("/v1/ask", json={**payload, "generation": "llm"}, headers=headers("maintainer"))
                    local_free = client.post("/v1/ask", json=payload, headers=headers("reader"))
                finally:
                    release.set()
                first_status = first.result(5).status_code
            assert first_status == 200 and conflicts == [429]*7
            assert llm_busy.status_code == 429 and local_free.status_code == 200 and selector.calls == 1
            assert app.state.admission.total == app.state.admission.llm == 0
        rate_app = create_app(AccessPolicy(AccessSettings(principals=entries, limits={"requests_per_minute": 2})))
        rate_app.state.rag_service = RagService(store)
        with TestClient(rate_app) as client:
            rate = [client.post("/v1/ask", json=payload, headers=headers("reader")).status_code for _ in range(3)]
        assert rate == [200, 200, 429]
        return {"transport": "in-process-asgi", "samples": "original-synthetic-policy", "real_llm": False,
                "selector": "controlled-local-test-double", "access_statuses": auth,
                "denied_write_preserved_revision": True, "same_principal_requests": 8,
                "accepted_responses": 1, "capacity_rejections": 7, "generation_calls": selector.calls,
                "other_principal_llm_when_full": llm_busy.status_code,
                "other_principal_local_when_llm_full": local_free.status_code,
                "slots_after_completion": app.state.admission.total, "rate_statuses": rate}


def main():
    print(json.dumps(demonstrate(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
