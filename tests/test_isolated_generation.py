from concurrent.futures import ThreadPoolExecutor
import asyncio
import io
import json
import subprocess
import threading
import sys

import pytest
import anyio

from compliance_rag import isolated_generation as isolation
from compliance_rag.generation import CompatibleSettings, ModelFailure, generator_from_env
from compliance_rag.isolated_generation import IsolatedGenerator, MAX_PACKET, WorkerLimits, WorkerRequest
from compliance_rag.provider_worker import run as worker_run
from compliance_rag.generation_demo import synthetic_provider

SECRET = "SYNTHETIC_WORKER_CREDENTIAL"
SETTINGS = CompatibleSettings("https://provider.invalid/v1", "synthetic-model", SECRET)
HITS = [{"citation_id": "C1", "evidence_text": "合成资料的进入记录保留180天。", "metadata": {"not_for_worker": "private-source-label"}}]
SELECTION = {"abstain": False, "excerpts": [{"citation_id": "C1", "quote": HITS[0]["evidence_text"]}]}


def envelope(selection=SELECTION):
    return json.dumps({"protocol": 1, "status": "ok", "selection": selection}, ensure_ascii=False).encode()


def reason(operation, expected):
    with pytest.raises(ModelFailure) as error:
        operation()
    assert str(error.value) == expected
    assert SECRET not in str(error.value)


class CapturedInput(io.BytesIO):
    def close(self):
        if not self.closed:
            self.captured = self.getvalue()
        super().close()


class FakeProcess:
    """Explicit lifecycle fault injection; not evidence of an unkillable real OS process."""
    def __init__(self, output=None, alive=False, unkillable=False, returncode=0):
        self.stdin, self.stdout = CapturedInput(), io.BytesIO(envelope() if output is None else output)
        self.alive, self.unkillable, self.code = alive, unkillable, returncode
        self.returncode = None if alive else returncode
        self.kills = 0
    def poll(self):
        self.returncode = None if self.alive else self.code
        return self.returncode
    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired("fixed synthetic worker", timeout)
        return self.poll()
    def kill(self):
        self.kills += 1
        if self.unkillable:
            raise OSError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
        self.alive, self.code = False, -1


def fake_launch(monkeypatch, process):
    calls = []
    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    return calls


def test_success_uses_fixed_worker_minimal_environment_and_private_pipe(monkeypatch):
    process = FakeProcess()
    calls = fake_launch(monkeypatch, process)
    monkeypatch.setenv("RAG_LLM_API_KEY", "SHOULD_NOT_BE_INHERITED")
    monkeypatch.setenv("RAG_ACCESS_JSON", "SHOULD_NOT_BE_INHERITED")
    monkeypatch.setenv("PYTHONPATH", "SHOULD_NOT_BE_INHERITED")
    generator = IsolatedGenerator(SETTINGS)
    assert generator.select("保留多久？", HITS) == SELECTION
    args, options = calls[0]
    assert args[0][1] == "-I" and args[0][2].endswith("provider_worker.py")
    assert SECRET not in str(args) and SECRET not in str(options)
    assert not ({"RAG_LLM_API_KEY", "RAG_ACCESS_JSON", "RAG_DB_PATH", "PYTHONPATH"} & options["env"].keys())
    assert options["stderr"] == subprocess.DEVNULL and not options.get("shell", False)
    sent = json.loads(process.stdin.captured)
    assert sent["provider"]["api_key"] == SECRET
    assert sent["evidence"] == [{"citation_id": "C1", "text": HITS[0]["evidence_text"]}]
    assert "private-source-label" not in process.stdin.captured.decode()
    assert generator.state() == {"occupied": 0, "quarantined": 0} and process.kills == 0


@pytest.mark.parametrize("changes", [{"deadline_seconds": True}, {"deadline_seconds": float("nan")},
    {"deadline_seconds": float("inf")}, {"deadline_seconds": .1}, {"deadline_seconds": 121},
    {"max_workers": True}, {"max_workers": 0}, {"max_workers": 9}, {"cleanup_seconds": 0}])
def test_invalid_worker_limits(changes):
    with pytest.raises(ValueError):
        WorkerLimits(**changes)


@pytest.mark.parametrize("name,value", [("RAG_LLM_DEADLINE_SECONDS", "PRIVATE_BAD_VALUE"),
    ("RAG_LLM_DEADLINE_SECONDS", "nan"), ("RAG_LLM_WORKERS", "1.2"), ("RAG_LLM_WORKERS", "0")])
def test_invalid_environment_does_not_echo_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        WorkerLimits.from_env()
    assert "PRIVATE_BAD_VALUE" not in str(error.value)


def test_environment_factory_uses_isolation_and_preserves_consent_gate(monkeypatch):
    monkeypatch.setenv("RAG_LLM_BASE_URL", SETTINGS.base_url)
    monkeypatch.setenv("RAG_LLM_MODEL", SETTINGS.model)
    monkeypatch.setenv("RAG_LLM_API_KEY", SECRET)
    monkeypatch.setenv("RAG_LLM_DEADLINE_SECONDS", "3")
    monkeypatch.setenv("RAG_LLM_WORKERS", "2")
    monkeypatch.delenv("RAG_ENABLE_EXTERNAL", raising=False)
    assert generator_from_env() is None
    monkeypatch.setenv("RAG_ENABLE_EXTERNAL", "1")
    result = generator_from_env()
    assert isinstance(result, IsolatedGenerator) and result.limits.deadline_seconds == 3 and result.limits.max_workers == 2


@pytest.mark.parametrize("question,hits", [("x"*501, HITS), (" ", HITS), ("问题", []),
    ("问题", HITS*2), ("问题", [{"citation_id": "C1", "evidence_text": "x"*2001}]),
    ("问题", [{"citation_id": f"C{i+1}", "evidence_text": "x"*2000} for i in range(4)])])
def test_bad_input_never_starts_a_process(monkeypatch, question, hits):
    calls = fake_launch(monkeypatch, FakeProcess())
    generator = IsolatedGenerator(SETTINGS)
    reason(lambda: generator.select(question, hits), "generation_worker_input_invalid")
    assert not calls and generator.state()["occupied"] == 0


@pytest.mark.parametrize("payload", [b"not json", b'\xff', b'{"protocol":1,"protocol":1}',
    json.dumps({"protocol": True, "status": "ok", "selection": SELECTION}).encode(),
    json.dumps({"protocol": 2, "status": "ok", "selection": SELECTION}).encode(),
    json.dumps({"protocol": 1, "status": "ok", "selection": SELECTION, "extra": SECRET}).encode(),
    json.dumps({"protocol": 1, "status": "ok", "selection": {"abstain": "false", "excerpts": []}}).encode(),
    json.dumps({"protocol": 1, "status": "error", "reason": SECRET}).encode()])
def test_bad_worker_reply_is_static_and_reaped(monkeypatch, payload):
    process = FakeProcess(payload)
    fake_launch(monkeypatch, process)
    generator = IsolatedGenerator(SETTINGS)
    reason(lambda: generator.select("问题", HITS), "generation_worker_protocol_error")
    assert process.stdin.closed and process.stdout.closed
    assert generator.state() == {"occupied": 0, "quarantined": 0}


def test_parent_response_read_is_bounded_and_terminates_overproducing_child(monkeypatch):
    process = FakeProcess(b"x"*(MAX_PACKET*4), alive=True)
    fake_launch(monkeypatch, process)
    generator = IsolatedGenerator(SETTINGS)
    reason(lambda: generator.select("问题", HITS), "generation_worker_response_limit")
    assert process.kills == 1 and generator.state()["occupied"] == 0


def test_upstream_failure_reason_is_preserved_without_body(monkeypatch):
    fake_launch(monkeypatch, FakeProcess(json.dumps({"protocol": 1, "status": "error", "reason": "upstream_http_error"}).encode()))
    reason(lambda: IsolatedGenerator(SETTINGS).select("问题", HITS), "upstream_http_error")


def test_child_nonzero_is_not_a_success_even_if_it_printed_valid_selection(monkeypatch):
    fake_launch(monkeypatch, FakeProcess(returncode=2))
    reason(lambda: IsolatedGenerator(SETTINGS).select("问题", HITS), "generation_worker_failed")


def test_spawn_failure_returns_no_details_and_releases_reservation(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(SECRET)
    monkeypatch.setattr(isolation.subprocess, "Popen", fail)
    generator = IsolatedGenerator(SETTINGS)
    reason(lambda: generator.select("问题", HITS), "generation_worker_start_failed")
    assert generator.state()["occupied"] == 0


def test_late_spawn_sends_no_input_and_rejects_late_success(monkeypatch):
    process = FakeProcess()
    now = [1.0]
    monkeypatch.setattr(isolation.time, "monotonic", lambda: now[0])
    def launch(*args, **kwargs):
        now[0] += 5
        return process
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    generator = IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=1))
    reason(lambda: generator.select("问题", HITS), "generation_deadline_exceeded")
    assert process.stdin.captured == b"" and generator.state()["occupied"] == 0


def test_cleanup_uncertainty_keeps_capacity_until_observed_exit(monkeypatch):
    process = FakeProcess(alive=True, unkillable=True)
    calls = fake_launch(monkeypatch, process)
    generator = IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=.25, cleanup_seconds=.05))
    reason(lambda: generator.select("问题", HITS), "generation_worker_cleanup_failed")
    assert generator.state() == {"occupied": 1, "quarantined": 1}
    reason(lambda: generator.select("问题", HITS), "generation_worker_busy")
    assert len(calls) == 1
    process.alive = False
    assert generator.state() == {"occupied": 0, "quarantined": 0}


def test_active_worker_capacity_has_no_wait_queue(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def launch(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return FakeProcess()
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    generator = IsolatedGenerator(SETTINGS)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(generator.select, "问题", HITS)
        try:
            assert entered.wait(2)
            reason(lambda: generator.select("问题", HITS), "generation_worker_busy")
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(3) == SELECTION
    assert generator.state()["occupied"] == 0


@pytest.mark.parametrize("raw", [b"x"*(MAX_PACKET+1), b'{}', b'{"protocol":1,"protocol":1}',
    b'{"protocol":true,"provider":{},"question":"x","evidence":[]}'],
    ids=["oversized", "missing-fields", "duplicate-json", "wrong-protocol-type"])
def test_invalid_child_input_never_echoes_or_fetches(raw):
    result = worker_run(raw)
    assert result == {"protocol": 1, "status": "error", "reason": "invalid_model_selection"}


def test_real_worker_calls_only_synthetic_http_and_reaps(monkeypatch):
    captured = []
    original = isolation.subprocess.Popen
    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        captured.append(child)
        return child
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    with synthetic_provider() as provider:
        generator = IsolatedGenerator(CompatibleSettings(provider.base_url, "synthetic-model", SECRET), WorkerLimits(deadline_seconds=6))
        assert generator.select("问题", HITS) == SELECTION
        assert provider.received == 1 and generator.state()["occupied"] == 0
        assert len(captured) == 1 and captured[0].poll() == 0


def test_real_header_stall_is_terminated_without_orphan(monkeypatch):
    captured = []
    original = isolation.subprocess.Popen
    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        captured.append(child)
        return child
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    with synthetic_provider() as provider:
        provider.set_mode("hang")
        generator = IsolatedGenerator(CompatibleSettings(provider.base_url, "synthetic-model", SECRET), WorkerLimits(deadline_seconds=6))
        reason(lambda: generator.select("问题", HITS), "generation_deadline_exceeded")
        assert provider.entered.is_set() and captured[0].poll() is not None
        assert generator.state() == {"occupied": 0, "quarantined": 0}


def test_demo_matches_fixed_evidence():
    from pathlib import Path
    from compliance_rag.generation_demo import demonstrate
    expected = Path(__file__).resolve().parents[1]/"examples/evaluation/generation-worker.json"
    assert demonstrate() == json.loads(expected.read_text(encoding="utf-8"))


def test_pipe_writer_cannot_block_supervisor_deadline(monkeypatch):
    """Owned real no-op child deliberately never consumes stdin; no network or model."""
    children = []
    def launch():
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if isolation.os.name == "nt" else 0)
        children.append(child)
        return child
    monkeypatch.setattr(IsolatedGenerator, "_launch", staticmethod(launch))
    hits = [{"citation_id": f"C{i+1}", "evidence_text": "合成文"*500} for i in range(4)]
    generator = IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=.5))
    reason(lambda: generator.select("问题", hits), "generation_deadline_exceeded")
    assert children[0].poll() is not None and generator.state()["occupied"] == 0


def test_io_thread_start_failure_reaps_the_already_created_child(monkeypatch):
    process = FakeProcess(alive=True)
    fake_launch(monkeypatch, process)
    def fail(_):
        raise RuntimeError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
    monkeypatch.setattr(isolation.threading.Thread, "start", fail)
    generator = IsolatedGenerator(SETTINGS)
    reason(lambda: generator.select("问题", HITS), "generation_worker_start_failed")
    assert process.kills == 1 and generator.state()["occupied"] == 0


def test_oversized_server_credential_error_does_not_echo_secret():
    with pytest.raises(ValueError) as error:
        IsolatedGenerator(CompatibleSettings(SETTINGS.base_url, SETTINGS.model, SECRET*300))
    assert SECRET not in str(error.value)


def test_process_exit_inspection_failure_quarantines_instead_of_freeing(monkeypatch):
    class PollFailure(FakeProcess):
        uncertain = True
        def poll(self):
            if self.uncertain:
                raise OSError(SECRET)
            return super().poll()
    process = PollFailure()
    fake_launch(monkeypatch, process)
    generator = IsolatedGenerator(SETTINGS, WorkerLimits(cleanup_seconds=.05))
    reason(lambda: generator.select("问题", HITS), "generation_worker_cleanup_failed")
    assert generator.state() == {"occupied": 1, "quarantined": 1}
    process.uncertain = False
    assert generator.state()["occupied"] == 0


def test_finished_but_late_response_is_never_published(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(isolation.time, "monotonic", lambda: now[0])
    class LateWait(FakeProcess):
        def wait(self, timeout=None):
            now[0] += 2
            return super().wait(timeout)
    fake_launch(monkeypatch, LateWait())
    reason(lambda: IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=1)).select("问题", HITS),
           "generation_deadline_exceeded")


def test_asgi_cancel_keeps_actual_child_and_admission_owned_until_deadline(monkeypatch):
    from compliance_rag.access import AccessBoundary, AccessPolicy, AccessSettings, Admission
    import hashlib
    token = "synthetic_access_" + "q"*43
    policy = AccessPolicy(AccessSettings(principals=[{"id": "test", "role": "analyst",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest()}]))
    gate = Admission(policy)
    launched, finished = threading.Event(), threading.Event()
    children, results = [], []
    def launch():
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if isolation.os.name == "nt" else 0)
        children.append(child)
        launched.set()
        return child
    monkeypatch.setattr(IsolatedGenerator, "_launch", staticmethod(launch))
    generator = IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=2))
    def worker(lease):
        try:
            with lease.work(external=True):
                generator.select("问题", HITS)
        except ModelFailure as error:
            results.append(str(error))
        finally:
            finished.set()
    async def run():
        async def downstream(scope, receive, send):
            await anyio.to_thread.run_sync(worker, scope["state"]["access_lease"], abandon_on_cancel=True)
        async def unused(*args):
            pass
        scope = {"type": "http", "method": "POST", "path": "/v1/ask",
                 "headers": [(b"authorization", ("Bearer "+token).encode())]}
        task = asyncio.create_task(AccessBoundary(downstream, policy, gate)(scope, unused, unused))
        assert await asyncio.to_thread(launched.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.total == gate.llm == generator.state()["occupied"] == 1
        assert await asyncio.to_thread(finished.wait, 5)
        assert children[0].poll() is not None
        assert gate.total == gate.llm == generator.state()["occupied"] == 0
        assert results == ["generation_deadline_exceeded"]
    asyncio.run(run())


def test_protocol_validation_cannot_publish_after_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(isolation.time, "monotonic", lambda: now[0])
    fake_launch(monkeypatch, FakeProcess())
    decode = IsolatedGenerator._decode
    def late_decode(output):
        result = decode(output)
        now[0] = 2
        return result
    monkeypatch.setattr(IsolatedGenerator, "_decode", staticmethod(late_decode))
    reason(lambda: IsolatedGenerator(SETTINGS, WorkerLimits(deadline_seconds=1)).select("问题", HITS),
           "generation_deadline_exceeded")
