"""Real owned subprocess + local synthetic HTTP provider; no genuine LLM or external service."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading

from fastapi.testclient import TestClient

from .access import AccessPolicy, AccessSettings
from .api import create_app
from .evaluation import seed
from .generation import CompatibleSettings, RagService
from .isolated_generation import IsolatedGenerator, WorkerLimits
from .knowledge import KnowledgeStore


class SyntheticProvider:
    """Transport fixture only. It deterministically copies evidence, never runs a model."""
    def __init__(self):
        self.mode = "ok"
        self.received, self.sent_bytes = 0, 0
        self.entered, self.stopped = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.connection.settimeout(10)
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/chat/completions" or not 0 < size <= 65536:
                    self.send_error(400)
                    return
                data = json.loads(self.rfile.read(size))
                with owner.lock:
                    owner.received += 1
                    mode = owner.mode
                owner.entered.set()
                if mode == "hang":
                    owner.stopped.wait(20)
                    return
                if mode == "http-error":
                    payload, status = b"SYNTHETIC_PROVIDER_DIAGNOSTIC", 429
                else:
                    evidence = json.loads(data["messages"][1]["content"])["untrusted_evidence"][0]
                    selection = {"abstain": False, "excerpts": [{"citation_id": evidence["citation_id"], "quote": evidence["text"]}]}
                    payload = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(selection)}}]}).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if mode == "drip":
                        # Each byte arrives far sooner than the worker's 30s read timeout.
                        for byte in payload:
                            if owner.stopped.wait(.05):
                                break
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            with owner.lock:
                                owner.sent_bytes += 1
                    else:
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # Expected when the exact owned worker is terminated.
                self.close_connection = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
        self.entered.clear()

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("synthetic provider did not stop")


@contextmanager
def synthetic_provider():
    provider = SyntheticProvider()
    try:
        yield provider
    finally:
        provider.close()


def demonstrate():
    with synthetic_provider() as provider, tempfile.TemporaryDirectory(prefix="rag-worker-") as directory:
        generator = IsolatedGenerator(CompatibleSettings(provider.base_url, "synthetic-copy-selector", secrets.token_urlsafe(32)),
                                      WorkerLimits(deadline_seconds=6))
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3")
        seed(store)
        token = secrets.token_urlsafe(32)
        app = create_app(AccessPolicy(AccessSettings(principals=[{"id": "demo", "role": "maintainer",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}])))
        app.state.rag_service = RagService(store, generator)
        payload = {"question": "进入记录保留多久？", "generation": "llm", "scope": {
            "standard_code": "TRAINING-POLICY", "edition": "v2", "security_level": "第三级", "security_class": "安全物理环境"}}
        with TestClient(app, headers={"Authorization": "Bearer "+token}) as client:
            success = client.post("/v1/ask", json=payload)
            assert success.status_code == 200 and "180天" in success.json()["answer"]
            assert generator.state() == {"occupied": 0, "quarantined": 0}
            provider.set_mode("drip")
            with ThreadPoolExecutor(1) as pool:
                request = pool.submit(client.post, "/v1/ask", json=payload)
                try:
                    assert provider.entered.wait(5)
                    assert generator.state()["occupied"] == app.state.admission.llm == 1
                    busy = client.post("/v1/ask", json=payload)
                    assert busy.status_code == 429
                    timed_out = request.result(9)
                finally:
                    # Ensure the fixture cannot continue dribbling indefinitely after a failed assertion.
                    provider.set_mode("ok")
            result = timed_out.json()
            assert timed_out.status_code == 200 and result["reason"] == "generation_deadline_exceeded"
            assert result["status"] == "refused" and result["answer"] == "" and result["citations"] == []
            assert provider.sent_bytes > 0
            assert generator.state() == {"occupied": 0, "quarantined": 0}
            assert app.state.admission.total == app.state.admission.llm == 0
            trace = store.trace(result["trace_id"])
            assert trace["reason"] == "generation_deadline_exceeded" and "question" not in trace
            recovered = client.post("/v1/ask", json=payload)
            assert recovered.status_code == 200 and "180天" in recovered.json()["answer"]
            assert generator.state() == {"occupied": 0, "quarantined": 0}
            return {"transport": "in-process-asgi-api+real-loopback-provider-http", "real_llm": False,
                "provider": "original-synthetic-copy-fixture", "worker": "actual-isolated-python-child",
                "deadline_seconds": 6, "success": "evidence_quotes", "bytes_arrived_before_deadline": True,
                "overlapping_request": busy.status_code, "timeout_reason": result["reason"],
                "timeout_answer": "", "timeout_citations": [], "occupied_after_timeout": 0,
                "quarantined_after_timeout": 0, "api_slots_after_timeout": 0,
                "trace_reason": trace["reason"], "next_request": "evidence_quotes"}


def main():
    print(json.dumps(demonstrate(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
