"""Real ephemeral loopback HTTP: ingest, ask, trace, remove, refusal; no external LLM."""
import json
import hashlib
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time

import httpx
import uvicorn


def main():
    from .api import create_app
    from .access import AccessPolicy, AccessSettings
    from .evaluation import DATA, sample_config
    from .generation import RagService
    from .knowledge import KnowledgeStore
    with tempfile.TemporaryDirectory(prefix="compliance-rag-http-") as directory:
        tokens = {role: secrets.token_urlsafe(32) for role in ("reader", "maintainer")}
        app = create_app(AccessPolicy(AccessSettings(principals=[{"id": role, "role": role,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for role, token in tokens.items()])))
        app.state.rag_service = RagService(KnowledgeStore(Path(directory)/"knowledge.sqlite3"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic()+10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError("local HTTP server did not start")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                unauthorized = client.get("/v1/library")
                reader_headers = {"Authorization": "Bearer " + tokens["reader"]}
                denied = client.post("/v1/library/ingest", json={}, headers=reader_headers)
                client.headers["Authorization"] = "Bearer " + tokens["maintainer"]
                indexed = client.post("/v1/library/ingest", json={"text":(DATA/"v2.md").read_bytes().decode(),
                    "source_name":"knowledge/v2.md", "config":sample_config("v2").model_dump()},
                    headers={"If-None-Match":"*"})
                assert indexed.status_code == 200 and indexed.json()["chunks"] == 6
                payload = {"question":"进入记录保留多久？","scope":{"standard_code":"TRAINING-POLICY",
                    "edition":"v2","security_level":"第三级","security_class":"安全物理环境"}}
                response = client.post("/v1/ask", json=payload)
                assert response.status_code == 200 and "180天" in response.json()["answer"]
                reader_ask = client.post("/v1/ask", json=payload, headers=reader_headers)
                reader_llm = client.post("/v1/ask", json={**payload, "generation": "llm"}, headers=reader_headers)
                assert [unauthorized.status_code,denied.status_code,reader_ask.status_code,reader_llm.status_code] == [401,403,200,403]
                trace = client.get("/v1/traces/"+response.json()["trace_id"])
                assert trace.status_code == 200 and "question" not in trace.json()
                edited = {"text":(DATA/"v2.md").read_bytes().decode().replace("180天","181天"),
                    "source_name":"knowledge/v2.md", "config":sample_config("v2").model_dump()}
                missing = client.post("/v1/library/ingest",json=edited)
                updated = client.post("/v1/library/ingest",json=edited,headers={"If-Match":indexed.headers["etag"]})
                stale = client.post("/v1/library/ingest",json=edited,headers={"If-Match":indexed.headers["etag"]})
                stale_delete = client.post("/v1/library/remove",json={"source_name":"knowledge/v2.md"},headers={"If-Match":indexed.headers["etag"]})
                assert [missing.status_code,updated.status_code,stale.status_code,stale_delete.status_code] == [428,200,412,412]
                latest = client.post("/v1/library/source",json={"source_name":"knowledge/v2.md"})
                assert "181天" in latest.json()["normalized_text"]
                assert latest.headers["etag"] == updated.headers["etag"] != indexed.headers["etag"]
                removed = client.post("/v1/library/remove",json={"source_name":"knowledge/v2.md"},
                                      headers={"If-Match": updated.headers["etag"]})
                empty = client.post("/v1/ask",json=payload)
                assert empty.json()["status"] == "refused" and not empty.json()["citations"]
                print(json.dumps({"transport":"real-loopback-http", "real_llm":False,
                    "statuses":[indexed.status_code,response.status_code,trace.status_code,removed.status_code,empty.status_code],
                    "indexed_chunks":6,"answer_contains":"180天", "after_delete":"no_evidence",
                    "conditional_statuses":[missing.status_code,updated.status_code,stale.status_code,stale_delete.status_code],
                    "access_statuses":[unauthorized.status_code,denied.status_code,reader_ask.status_code,reader_llm.status_code],
                    "latest_source_contains":"181天", "etag_changed":True}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()
            if thread.is_alive():
                raise RuntimeError("local HTTP server did not stop")


if __name__ == "__main__":
    main()
