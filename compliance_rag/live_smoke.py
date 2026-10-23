"""Real ephemeral loopback HTTP: ingest, ask, trace, remove, refusal; no external LLM."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

import httpx
import uvicorn


def main():
    from .api import app
    from .evaluation import DATA, sample_config
    from .generation import RagService
    from .knowledge import KnowledgeStore
    with tempfile.TemporaryDirectory(prefix="compliance-rag-http-") as directory:
        previous = app.state.rag_service
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
                indexed = client.post("/v1/library/ingest", json={"text":(DATA/"v2.md").read_bytes().decode(),
                    "source_name":"knowledge/v2.md", "config":sample_config("v2").model_dump()})
                assert indexed.status_code == 200 and indexed.json()["chunks"] == 6
                payload = {"question":"进入记录保留多久？","scope":{"standard_code":"TRAINING-POLICY",
                    "edition":"v2","security_level":"第三级","security_class":"安全物理环境"}}
                response = client.post("/v1/ask", json=payload)
                assert response.status_code == 200 and "180天" in response.json()["answer"]
                trace = client.get("/v1/traces/"+response.json()["trace_id"])
                assert trace.status_code == 200 and "question" not in trace.json()
                removed = client.post("/v1/library/remove",json={"source_name":"knowledge/v2.md"})
                empty = client.post("/v1/ask",json=payload)
                assert empty.json()["status"] == "refused" and not empty.json()["citations"]
                print(json.dumps({"transport":"real-loopback-http", "real_llm":False,
                    "statuses":[indexed.status_code,response.status_code,trace.status_code,removed.status_code,empty.status_code],
                    "indexed_chunks":6,"answer_contains":"180天", "after_delete":"no_evidence"}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()
            app.state.rag_service = previous
            if thread.is_alive():
                raise RuntimeError("local HTTP server did not stop")


if __name__ == "__main__":
    main()
