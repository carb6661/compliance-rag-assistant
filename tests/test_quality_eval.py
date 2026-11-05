"""Adversarial scorer/replay tests, not just a snapshot of the production retriever."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from compliance_rag import quality_eval as q
from compliance_rag.generation import AskRequest, RagService
from compliance_rag.knowledge import KnowledgeStore


@pytest.fixture(scope="module")
def suite():
    return q.load_suite()


@pytest.fixture(scope="module")
def observations(suite):
    return q.run_local(suite)


def refusal(reason="model_abstained"):
    return {"status": "refused", "reason": reason, "answer": "", "citations": [], "retrieval": {"hits": []}}


def render(response):
    response["answer"] = q.ANSWER_PREFIX + "\n".join(f'[{c["citation_id"]}] {c["quote"]}' for c in response["citations"])


def row(observations, index=0):
    return deepcopy(observations["cases"][index]["response"])


def test_actual_baseline_exposes_valid_quotes_without_answers(suite, observations):
    report = q.score(suite, observations)
    assert report["metrics"] == {"answerable_cases": 10, "unanswerable_cases": 8,
        "all_facts_retrieved_rate": 1.0, "fact_complete_answer_rate": 1.0,
        "unanswerable_semantic_refusal_rate": 0.25, "unsupported_answer_cases": 6,
        "operational_refusal_cases": 0, "invalid_response_cases": 0}
    assert report["gate"]["passed"] is False
    assert all(r["citation_check"] == "valid" for r in report["cases"] if not r["passed"])
    assert report == json.loads(Path("examples/evaluation/quote-quality.json").read_text(encoding="utf-8"))


def test_oracle_annotations_never_cross_request_boundary(suite):
    seen = []
    def target(request):
        assert isinstance(request, AskRequest)
        assert set(request.model_dump()) == {"question", "scope", "retrieval", "top_k", "generation"}
        seen.append(request.model_dump())
        request.scope.edition = "MUTATED_TARGET_COPY"
        return refusal()
    q.capture(suite, target)
    assert len(seen) == 18 and all(c.scope.edition != "MUTATED_TARGET_COPY" for c in suite.spec.cases)
    assert "facts" not in json.dumps(seen) and "rationale" not in json.dumps(seen)


def test_offline_default_ignores_external_environment(suite, monkeypatch):
    monkeypatch.setenv("RAG_ENABLE_EXTERNAL", "1")
    monkeypatch.setattr(q, "generator_from_env", lambda: pytest.fail("offline evaluation attempted external config"))
    q.run_local(suite)


def test_missing_explicit_llm_is_configuration_error_not_a_refusal_score(suite, monkeypatch):
    monkeypatch.setattr(q, "generator_from_env", lambda: None)
    with pytest.raises(ValueError):
        q.run_local(suite, generation="llm")


@pytest.mark.parametrize("reason", ["model_not_configured", "generation_deadline", "upstream_http_error", "knowledge_changed"])
def test_operational_failure_never_scores_as_successful_refusal(suite, reason):
    result = q.score_case(suite.spec.cases[-1], refusal(reason), suite)
    assert not result["passed"] and result["operational_refusal"] and not result["semantic_refusal"]


def test_always_refusing_cannot_pass_release_gate(suite, observations):
    altered = deepcopy(observations)
    for item in altered["cases"]:
        item["response"] = refusal()
    report = q.score(suite, altered)
    assert report["metrics"]["unanswerable_semantic_refusal_rate"] == 1
    assert report["metrics"]["fact_complete_answer_rate"] == 0
    assert report["cases"][0]["citation_check"] == "not_applicable"
    assert len(report["gate"]["failed_cases"]) == 10


@pytest.mark.parametrize("field,value,code", [
    ("start_char", True, "span_mismatch"), ("end_char", -1, "span_mismatch"),
    ("end_char", 999999, "span_mismatch"), ("start_line", True, "line_mismatch"),
    ("end_line", 999, "line_mismatch"), ("source_sha256", "0"*64, "source_hash_mismatch"),
    ("source", "../private.md", "unknown_source"), ("quote", "伪造的连续原文内容", "span_mismatch"),
    ("citation_id", ["C1"], "citation_identity"), ("chunk_id", "0"*64, "provenance_identity"),
    ("source_etag", "fake", "provenance_identity"),
])
def test_tampered_citation_fails_closed(suite, observations, field, value, code):
    response = row(observations)
    response["citations"][0][field] = value
    result = q.score_case(suite.spec.cases[0], response, suite)
    assert not result["passed"] and code in result["diagnostics"]


def test_echoed_metadata_cannot_override_reviewed_document_scope(suite, observations):
    response = row(observations, 5)  # Valid v1 source, request v2.
    for item in response["citations"] + response["retrieval"]["hits"]:
        item["metadata"]["标准版本"] = "v2"
    result = q.score_case(suite.spec.cases[0], response, suite)
    assert not result["passed"]
    assert {"wrong_scope", "metadata_mismatch"} <= set(result["diagnostics"])


def test_real_source_span_not_retrieved_is_invalid(suite, observations):
    response = row(observations)
    response["retrieval"]["hits"] = []
    result = q.score_case(suite.spec.cases[0], response, suite)
    assert not result["passed"] and "citation_not_retrieved" in result["diagnostics"]


def test_retrieved_all_facts_but_partial_quote_does_not_pass(suite, observations):
    response = row(observations, 2)
    citation = response["citations"][0]
    citation["quote"] = "进入记录保留210天。"
    text = suite.texts[citation["source"]]
    citation["start_char"] = text.index(citation["quote"])
    citation["end_char"] = citation["start_char"] + len(citation["quote"])
    citation["start_line"] = text[:citation["start_char"]].count("\n") + 1
    citation["end_line"] = text[:citation["end_char"]-1].count("\n") + 1
    response["citations"] = [citation]
    render(response)
    result = q.score_case(suite.spec.cases[2], response, suite)
    assert result["retrieved_facts"] == ["retention", "review"]
    assert result["quoted_facts"] == ["retention"]
    assert result["citation_check"] == "valid" and not result["fact_complete"]


def test_valid_quote_cannot_hide_freeform_unsupported_answer(suite, observations):
    response = row(observations)
    response["answer"] += "\n所以该组织通过测评。"
    result = q.score_case(suite.spec.cases[0], response, suite)
    assert not result["passed"] and "answer_contract" in result["diagnostics"]


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "leaking-refusal", "lying-no-evidence", "null-metadata", "bad-hit-id"])
def test_malformed_and_inconsistent_answer_contracts(suite, observations, mutation):
    response = row(observations)
    if mutation == "empty":
        response["citations"] = []
    elif mutation == "duplicate":
        response["citations"].append(deepcopy(response["citations"][0]))
        render(response)
    elif mutation == "leaking-refusal":
        response["status"], response["reason"] = "refused", "model_abstained"
    elif mutation == "lying-no-evidence":
        response.update(status="refused", reason="no_evidence", answer="", citations=[])
    elif mutation == "null-metadata":
        response["retrieval"]["hits"][0]["metadata"] = None
    else:
        response["retrieval"]["hits"][0]["citation_id"] = []
    assert not q.score_case(suite.spec.cases[0], response, suite)["passed"]


def test_two_runs_compare_without_reusing_summary_scores(suite, observations):
    candidate = deepcopy(observations)
    # Test-only label-informed mutation: NOT an implemented target/model improvement.
    candidate["cases"][10]["response"] = refusal()
    candidate["cases"][0]["response"] = refusal()
    report = q.compare(suite, observations, candidate)
    assert report["improved_cases"] == ["n01-algorithm"]
    assert report["regressed_cases"] == ["p01-retention"]
    assert report["metric_deltas"]["unanswerable_semantic_refusal_rate"] == 0.125
    assert not report["gate"]["passed"] and not report["gate"]["no_case_regressions"]


def test_namespaced_real_model_identity_is_valid_declared_metadata(suite, observations):
    data = deepcopy(observations)
    data["target"]["embedding_identity"] = "BAAI/bge-small-zh-v1.5:fastembed-0.9.0:" + "a"*64
    assert q.score(suite, data)["target"]["embedding_identity"] == data["target"]["embedding_identity"]


@pytest.mark.parametrize("mutation", ["hash", "missing", "duplicate", "unknown", "target", "reported-summary"])
def test_wrong_replay_inventory_rejected(suite, observations, mutation):
    data = deepcopy(observations)
    if mutation == "hash":
        data["suite_sha256"] = "0"*64
    elif mutation == "missing":
        data["cases"].pop()
    elif mutation == "duplicate":
        data["cases"][1]["id"] = data["cases"][0]["id"]
    elif mutation == "unknown":
        data["cases"][0]["id"] = "not-in-suite"
    elif mutation == "target":
        data["target"]["id"] = "PRIVATE_CONTENT\n"
    else:
        data = q.score(suite, observations)
    with pytest.raises(ValueError):
        q.score(suite, data)


@pytest.mark.parametrize("mutation", ["hash", "duplicate-case", "anchor-missing", "anchor-scope", "traversal", "no-negative", "refusal-facts"])
def test_reviewed_suite_validation(tmp_path, mutation):
    original = json.loads(q.DEFAULT_SUITE.read_text(encoding="utf-8"))
    for source in q.DEFAULT_SUITE.parent.glob("*.md"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    if mutation == "hash":
        original["sources"][0]["sha256"] = "0"*64
    elif mutation == "duplicate-case":
        original["cases"][1]["id"] = original["cases"][0]["id"]
    elif mutation == "anchor-missing":
        original["cases"][0]["facts"][0]["any_of"][0]["quote"] = "不存在的原文锚点"
    elif mutation == "anchor-scope":
        original["cases"][0]["scope"]["edition"] = "v1"
    elif mutation == "traversal":
        original["sources"][0]["file"] = "../private.md"
    elif mutation == "no-negative":
        original["cases"] = original["cases"][:10]
    else:
        original["cases"][-1]["facts"] = original["cases"][0]["facts"]
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        q.load_suite(path)


def test_normalized_hash_survives_bom_crlf_and_unicode_offsets(tmp_path, suite):
    for source in q.DEFAULT_SUITE.parent.glob("*.md"):
        text = q.normalize_text(source.read_text(encoding="utf-8"))
        (tmp_path / source.name).write_bytes(("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8"))
    (tmp_path / "suite.json").write_bytes(q.DEFAULT_SUITE.read_bytes())
    loaded = q.load_suite(tmp_path / "suite.json")
    assert loaded.fingerprint == suite.fingerprint
    assert q.score(loaded, q.run_local(loaded))["metrics"]["invalid_response_cases"] == 0


def test_read_limits_and_duplicate_json_keys(tmp_path):
    file = tmp_path / "data.json"
    for text, limit in [("{}"*100, 16), ('{"x":1,"x":2}', 64), ('{"x":NaN}', 64)]:
        file.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            q.read_json(file, limit)


@pytest.mark.parametrize("response", [None, [], {}, {"status": [], "reason": []},
    {"status": "evidence", "reason": [], "answer": "", "citations": [], "retrieval": {"hits": []}}])
def test_non_contract_responses_are_failures_not_scorer_crashes(suite, response):
    result = q.score_case(suite.spec.cases[0], response, suite)
    assert not result["passed"] and result["status"] == "invalid"


def test_scorer_does_not_echo_provider_reason_or_question(suite, observations):
    data = deepcopy(observations)
    data["cases"][0]["response"] = refusal("PRIVATE_PROVIDER_REASON")
    encoded = json.dumps(q.score(suite, data), ensure_ascii=False)
    assert "PRIVATE_PROVIDER_REASON" not in encoded and suite.spec.cases[0].question not in encoded


def test_cli_gate_exit_replay_and_output_protection(tmp_path, capsys):
    responses, report = tmp_path / "responses.json", tmp_path / "report.json"
    assert q.main(["run", "--responses", str(responses), "--out", str(report), "--gate"]) == 3
    before = report.read_bytes()
    assert q.main(["score", "--responses", str(responses), "--out", str(report)]) == 2
    assert report.read_bytes() == before
    assert q.main(["score", "--responses", str(responses), "--gate"]) == 3
    assert q.main(["compare", "--responses", str(responses), "--baseline", str(responses), "--gate"]) == 3
    assert q.main(["score", "--responses", str(responses), "--out", str(responses)]) == 2
    assert q.main(["run", "--out", str(q.DEFAULT_SUITE)]) == 2
    assert q.main(["score"]) == 2
    assert "PRIVATE_PROVIDER_REASON" not in capsys.readouterr().out


def test_declared_scope_disagreement_is_not_silently_ingested(tmp_path, suite):
    modified = deepcopy(suite)
    modified.spec.sources[0].scope.security_class = "安全通信网络"
    store = KnowledgeStore(tmp_path / "wrong.sqlite3")
    with pytest.raises(ValueError, match="parsed scope"):
        q.seed_quality(store, modified)
