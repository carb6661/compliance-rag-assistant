"""Source-bound quote-contract evaluation, separate from target requests and old tuning data.

This is an offline developer tool, not a semantic judge or a service endpoint.
Gold annotations never enter the AskRequest or generator context.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .__main__ import atomic_write
from .conditions import WriteCondition
from .generation import AskRequest, RagService, generator_from_env, strict_json
from .knowledge import KnowledgeStore, Scope
from .models import IngestConfig
from .parser import canonical_json, normalize_text, sha

DEFAULT_SUITE = Path(__file__).resolve().parent.parent / "examples/quality/suite.json"
SEMANTIC_REFUSALS = {"no_evidence", "model_abstained"}
ANSWER_PREFIX = "以下为所选范围内的原文证据，不构成测评结论：\n"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Record):
    file: str = Field(pattern=r"^[a-z0-9-]{1,64}\.md$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Scope


class Anchor(Record):
    source: str
    quote: str = Field(min_length=4, max_length=2000)


class Fact(Record):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    any_of: list[Anchor] = Field(min_length=1, max_length=5)


class Case(Record):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    question: str
    scope: Scope
    expect: Literal["answer", "refuse"]
    category: Literal["fact", "multi-fact", "related-missing", "partial-missing", "judgement", "absent-scope"]
    rationale: str = Field(min_length=1, max_length=1000)
    facts: list[Fact] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent(self):
        AskRequest(question=self.question, scope=self.scope)
        if bool(self.facts) != (self.expect == "answer") or len({f.id for f in self.facts}) != len(self.facts):
            raise ValueError("inconsistent gold facts")
        return self


class Suite(Record):
    format: Literal["quote-eval-v1"]
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    authorship: Literal["ai-assisted-original-synthetic-not-independent-human-labels"]
    standard_name: str = Field(min_length=1, max_length=256)
    sources: list[Source] = Field(min_length=1, max_length=16)
    cases: list[Case] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def unique(self):
        if (len({s.file for s in self.sources}) != len(self.sources)
                or len({c.id for c in self.cases}) != len(self.cases)
                or {c.expect for c in self.cases} != {"answer", "refuse"}):
            raise ValueError("duplicate IDs or missing positive/negative cases")
        return self


@dataclass(frozen=True)
class LoadedSuite:
    spec: Suite
    texts: dict
    fingerprint: str


def read_json(path, limit):
    if path.is_symlink():
        raise ValueError("symlink input rejected")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input budget exceeded")
    return strict_json(data.decode("utf-8"))


def in_scope(source_scope, query_scope):
    return (source_scope.standard_code == query_scope.standard_code
        and source_scope.edition == query_scope.edition
        and source_scope.security_level == query_scope.security_level
        and (query_scope.security_class is None or source_scope.security_class == query_scope.security_class))


def load_suite(path=DEFAULT_SUITE):
    path = Path(path)
    spec = Suite.model_validate(read_json(path, 262144))
    texts = {}
    for source in spec.sources:
        file = path.parent / source.file
        if file.is_symlink() or not file.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("source outside suite")
        with file.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("source budget exceeded")
        text = normalize_text(raw.decode("utf-8"))
        if sha(text.encode("utf-8")) != source.sha256 or source.scope.security_class is None:
            raise ValueError("source hash or class does not match reviewed manifest")
        texts[source.file] = text
    sources = {s.file: s for s in spec.sources}
    for case in spec.cases:
        for fact in case.facts:
            for anchor in fact.any_of:
                source = sources.get(anchor.source)
                if (source is None or not in_scope(source.scope, case.scope)
                        or texts[anchor.source].count(anchor.quote) != 1):
                    raise ValueError("gold anchor must be unique, present and in scope")
    # Source hashes bind all normalized bytes; annotations and scope are also bound.
    return LoadedSuite(spec, texts, sha(canonical_json(spec.model_dump()).encode("utf-8")))


def seed_quality(store, suite):
    for source in suite.spec.sources:
        config = IngestConfig.model_validate({"standards": {suite.spec.standard_name: {
            "name": suite.spec.standard_name, "code": source.scope.standard_code,
            "edition": source.scope.edition, "synthetic": True}}, "max_chars": 300})
        store.ingest(suite.texts[source.file], source.file, config, condition=WriteCondition.create())
    # A declared source has exactly one scope. Fail if parser and reviewed ledger disagree.
    declarations = {s.file: s.scope for s in suite.spec.sources}
    for row in store.inventory()["sources"]:
        scope = declarations[row["source"]]
        if (row["standard_code"], row["edition"], row["level"], row["security_class"]) != (
                scope.standard_code, scope.edition, scope.security_level, scope.security_class):
            raise ValueError("parsed scope disagrees with source ledger")


def capture(suite, target, *, retrieval="bm25", generation="extractive", target_id="injected-test-target", embedding_identity="none"):
    """Only a fresh request crosses this callable boundary, never Case/Fact/Anchor.

    This API separation is not a sandbox against a target deliberately reading local gold files.
    """
    cases = []
    for case in suite.spec.cases:
        request = AskRequest(question=case.question, scope=case.scope.model_copy(deep=True),
                             retrieval=retrieval, generation=generation)
        response = target(request)
        cases.append({"id": case.id, "response": response})
    return {"format": "quote-observations-v1", "suite_sha256": suite.fingerprint,
            "target": {"id": target_id, "retrieval": retrieval, "generation": generation,
                       "embedding_identity": embedding_identity}, "cases": cases}


def run_local(suite, *, embedder=None, retrieval="bm25", generation="extractive"):
    # The offline default never invokes generator_from_env, even if external env exists.
    generator = generator_from_env() if generation == "llm" else None
    if generation == "llm" and generator is None:
        raise ValueError("explicit external generation is not configured")
    with tempfile.TemporaryDirectory(prefix="rag-quality-") as directory:
        store = KnowledgeStore(Path(directory) / "quality.sqlite3", embedder)
        seed_quality(store, suite)
        return capture(suite, RagService(store, generator).ask, retrieval=retrieval,
                       generation=generation, target_id=f"compliance-rag-{__version__}", embedding_identity=store.identity)


def integer(value):
    return type(value) is int


def verify_span(item, suite, scope, *, hit=False):
    """Independent byte-hash + Python-character/line replay; never trust echoed metadata.

    Returns a trusted (source,start,end) interval or static diagnostic codes.
    """
    errors = set()
    if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
        return None, {"span_schema"}
    meta = item["metadata"]
    source = meta.get("来源文件") if hit else item.get("source")
    declarations = {s.file: s for s in suite.spec.sources}
    if not isinstance(source, str) or source not in declarations:
        return None, {"unknown_source"}
    declared, text = declarations[source], suite.texts[source]
    if not in_scope(declared.scope, scope):
        errors.add("wrong_scope")
    expected_meta = {"来源文件": source, "标准名称": suite.spec.standard_name,
        "标准编号": declared.scope.standard_code, "标准版本": declared.scope.edition,
        "安全级别": declared.scope.security_level, "安全类": declared.scope.security_class,
        "源文件SHA256": declared.sha256}
    if any(meta.get(k) != v for k, v in expected_meta.items()):
        errors.add("metadata_mismatch")
    if not hit and item.get("source_sha256") != declared.sha256:
        errors.add("source_hash_mismatch")
    if (not isinstance(item.get("chunk_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["chunk_id"])
            or item["chunk_id"] != meta.get("块ID")
            or not isinstance(item.get("source_etag"), str)
            or not re.fullmatch(r'"[0-9a-f]{64}"', item["source_etag"])):
        errors.add("provenance_identity")
    start = meta.get("起始字符") if hit else item.get("start_char")
    end = start + len(item["evidence_text"]) if hit and integer(start) and isinstance(item.get("evidence_text"), str) else (
        None if hit else item.get("end_char"))
    quote = item.get("evidence_text") if hit else item.get("quote")
    if (not integer(start) or not integer(end) or not isinstance(quote, str) or not quote.strip()
            or len(quote) > 2000 or (not hit and len(quote) < 4)
            or not 0 <= start < end <= len(text) or text[start:end] != quote):
        return None, errors | {"span_mismatch"}
    line_start = text[:start].count("\n") + 1
    line_end = text[:end-1].count("\n") + 1
    if hit:
        if meta.get("起始行") != line_start or not integer(meta.get("起始行")):
            errors.add("line_mismatch")
        if not integer(meta.get("结束字符")) or not end <= meta["结束字符"] <= len(text):
            errors.add("span_mismatch")
        elif (not integer(meta.get("结束行"))
              or meta["结束行"] != text[:meta["结束字符"]-1].count("\n") + 1):
            errors.add("line_mismatch")
    elif (not integer(item.get("start_line")) or not integer(item.get("end_line"))
            or item["start_line"] != line_start or item["end_line"] != line_end):
        errors.add("line_mismatch")
    return (source, start, end) if not errors else None, errors


def covered(fact, intervals, suite):
    for anchor in fact.any_of:
        start = suite.texts[anchor.source].index(anchor.quote)
        if any(source == anchor.source and a <= start and b >= start + len(anchor.quote)
               for source, a, b in intervals):
            return True
    return False


def score_case(case, response, suite):
    if not isinstance(response, dict) or len(canonical_json(response).encode("utf-8")) > 131072:
        response = {}
    citations, hits = response.get("citations"), response.get("retrieval", {})
    hits = hits.get("hits") if isinstance(hits, dict) else None
    diagnostics, spans, hit_spans = set(), [], []
    if (response.get("status") not in ("evidence", "refused") or not isinstance(response.get("reason"), str)
            or not isinstance(response.get("answer"), str) or not isinstance(citations, list)
            or len(citations) > 5 or not isinstance(hits, list) or len(hits) > 5):
        diagnostics.add("response_schema")
        citations, hits = [], []
        response = {"status": "invalid", "reason": None, "answer": ""}
    hit_ids = set()
    trusted_hits = []
    for hit in hits:
        interval, errors = verify_span(hit, suite, case.scope, hit=True)
        if (not isinstance(hit, dict) or not isinstance(hit.get("citation_id"), str)
                or not re.fullmatch(r"C[1-5]", hit["citation_id"]) or hit["citation_id"] in hit_ids):
            errors.add("hit_identity")
        elif isinstance(hit, dict):
            hit_ids.add(hit["citation_id"])
        diagnostics.update(errors)
        if interval and not errors:
            hit_spans.append(interval)
            trusted_hits.append((hit, interval))
    seen = set()
    for item in citations:
        interval, errors = verify_span(item, suite, case.scope)
        diagnostics.update(errors)
        if not isinstance(item, dict):
            continue
        citation_id = item.get("citation_id")
        key = (citation_id, interval) if isinstance(citation_id, str) else None
        if (key is None or not re.fullmatch(r"C[1-5]", citation_id) or key in seen):
            diagnostics.add("citation_identity")
        if key is not None:
            seen.add(key)
        # A genuine source span must also belong to this response's retrieved evidence.
        matched = any(interval and h.get("citation_id") == citation_id
            and h.get("chunk_id") == item.get("chunk_id")
            and isinstance(item.get("quote"), str)
            and item["quote"] in h["evidence_text"] and h.get("source_etag") == item.get("source_etag")
            and h["metadata"] == item.get("metadata") and h_span[0] == interval[0]
            and h_span[1] <= interval[1] < interval[2] <= h_span[2] for h, h_span in trusted_hits)
        if not matched:
            diagnostics.add("citation_not_retrieved")
        if interval:
            spans.append(interval)
    if response.get("status") == "evidence":
        if response.get("reason") != "evidence_quotes":
            diagnostics.add("answer_contract")
        if not citations:
            diagnostics.add("empty_evidence")
        elif all(isinstance(c, dict) and isinstance(c.get("quote"), str) for c in citations):
            expected_answer = ANSWER_PREFIX + "\n".join(f'[{c.get("citation_id")}] {c["quote"]}' for c in citations)
            if response.get("answer") != expected_answer:
                diagnostics.add("answer_contract")
    elif response.get("answer") or citations:
        diagnostics.add("refusal_leaks_answer")
    if response.get("status") == "refused" and response.get("reason") == "no_evidence" and hits:
        diagnostics.add("refusal_reason_contract")
    facts_hit = [fact.id for fact in case.facts if covered(fact, hit_spans, suite)]
    facts_quoted = [fact.id for fact in case.facts if covered(fact, spans, suite)]
    clean = not diagnostics
    complete = (clean and response.get("status") == "evidence" and bool(case.facts)
                and len(facts_quoted) == len(case.facts))
    semantic_refusal = (clean and response.get("status") == "refused"
                        and response.get("reason") in SEMANTIC_REFUSALS)
    passed = complete if case.expect == "answer" else semantic_refusal
    return {"id": case.id, "category": case.category, "expected": case.expect,
        "status": response.get("status") if response.get("status") in ("evidence", "refused") else "invalid",
        "fact_count": len(case.facts), "retrieved_facts": facts_hit, "quoted_facts": facts_quoted,
        "citation_check": "invalid" if diagnostics else ("valid" if citations else "not_applicable"),
        "diagnostics": sorted(diagnostics), "fact_complete": complete,
        "semantic_refusal": semantic_refusal,
        "operational_refusal": response.get("status") == "refused" and response.get("reason") not in SEMANTIC_REFUSALS,
        "passed": passed}


def score(suite, observations):
    if (not isinstance(observations, dict) or set(observations) != {"format", "suite_sha256", "target", "cases"}
            or observations["format"] != "quote-observations-v1" or observations["suite_sha256"] != suite.fingerprint
            or not isinstance(observations["cases"], list) or len(observations["cases"]) != len(suite.spec.cases)):
        raise ValueError("observation manifest mismatch")
    target = observations["target"]
    if (not isinstance(target, dict) or set(target) != {"id", "retrieval", "generation", "embedding_identity"}
            or not isinstance(target["id"], str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", target["id"])
            or target["retrieval"] not in ("bm25", "dense", "hybrid") or target["generation"] not in ("extractive", "llm")
            or not isinstance(target["embedding_identity"], str)
            or not re.fullmatch(r"[a-zA-Z0-9/:._-]{1,256}", target["embedding_identity"])):
        raise ValueError("invalid target manifest")
    responses = {}
    for row in observations["cases"]:
        if not isinstance(row, dict) or set(row) != {"id", "response"} or not isinstance(row["id"], str) or row["id"] in responses:
            raise ValueError("duplicate or malformed observation")
        responses[row["id"]] = row["response"]
    if set(responses) != {c.id for c in suite.spec.cases}:
        raise ValueError("case inventory mismatch")
    results = [score_case(c, responses[c.id], suite) for c in suite.spec.cases]
    positive = [r for r in results if r["expected"] == "answer"]
    negative = [r for r in results if r["expected"] == "refuse"]
    metrics = {"answerable_cases": len(positive), "unanswerable_cases": len(negative),
        "all_facts_retrieved_rate": sum(len(r["retrieved_facts"]) == r["fact_count"] for r in positive) / len(positive),
        "fact_complete_answer_rate": sum(r["fact_complete"] for r in positive) / len(positive),
        "unanswerable_semantic_refusal_rate": sum(r["semantic_refusal"] for r in negative) / len(negative),
        "unsupported_answer_cases": sum(r["status"] == "evidence" for r in negative),
        "operational_refusal_cases": sum(r["operational_refusal"] for r in results),
        "invalid_response_cases": sum(bool(r["diagnostics"]) for r in results)}
    failed = [r["id"] for r in results if not r["passed"]]
    return {"format": "quote-quality-report-v1", "suite": suite.spec.id, "suite_sha256": suite.fingerprint,
        "authorship": suite.spec.authorship, "target": target, "metrics": metrics,
        "gate": {"policy": "all-positive-facts-and-all-negative-semantic-refusals-with-valid-provenance",
                 "passed": not failed, "failed_cases": failed}, "cases": results,
        "limitations": ["AI-assisted synthetic regression suite; not third-party, human-adjudicated or held-out production evidence.",
            "Exact annotated phrase coverage for quote-contract answers, not general semantic entailment or compliance truth.",
            "Target identity is declared; replay cannot attest a real model call, ACL enforcement or absence of local gold-file access."]}


def compare(suite, baseline, candidate):
    """Re-score both raw inventories; never trust a supplied aggregate report."""
    before, after = score(suite, baseline), score(suite, candidate)
    pairs = list(zip(before["cases"], after["cases"]))
    regressed = [a["id"] for b, a in pairs if b["passed"] and not a["passed"]]
    improved = [a["id"] for b, a in pairs if not b["passed"] and a["passed"]]
    return {"format": "quote-quality-comparison-v1", "suite_sha256": suite.fingerprint,
        "baseline": before, "candidate": after, "improved_cases": improved, "regressed_cases": regressed,
        "metric_deltas": {key: after["metrics"][key] - value for key, value in before["metrics"].items()},
        "gate": {"passed": after["gate"]["passed"] and not regressed,
                 "candidate_passed": after["gate"]["passed"], "no_case_regressions": not regressed}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="来源绑定的事实覆盖/拒答评测；报告成功不等于质量门禁通过")
    parser.add_argument("command", choices=["run", "score", "compare"])
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--responses", type=Path, help="score读取/run可选写出；含原文，只存本地")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--baseline", type=Path, help="compare重新评分的基线原始观测；不能用汇总分数替代")
    parser.add_argument("--gate", action="store_true", help="质量不通过时退出3；默认只生成诊断报告")
    parser.add_argument("--retrieval", choices=["bm25", "dense", "hybrid"], default="bm25")
    parser.add_argument("--generation", choices=["extractive", "llm"], default="extractive")
    parser.add_argument("--model-path")
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite)
        # Output creation never replaces existing files; also forbid collisions with reviewed inputs.
        protected = {args.suite.resolve(), *[(args.suite.parent / s.file).resolve() for s in suite.spec.sources]}
        from .__main__ import check_outputs
        outputs = [args.out] + ([args.responses] if args.command == "run" else [])
        if args.command != "run":
            if not args.responses:
                raise ValueError("score needs observations")
            protected.add(args.responses.resolve())
        if args.command == "compare":
            if not args.baseline:
                raise ValueError("compare needs baseline observations")
            protected.add(args.baseline.resolve())
        check_outputs(outputs, protected, False)
        if args.command == "run":
            embedder = None
            if args.model_path:
                from .embeddings import ChineseEmbedder
                embedder = ChineseEmbedder(args.model_path)
            observations = run_local(suite, embedder=embedder, retrieval=args.retrieval, generation=args.generation)
        else:
            observations = read_json(args.responses, 16777216)
        report = (compare(suite, read_json(args.baseline, 16777216), observations)
                  if args.command == "compare" else score(suite, observations))
        if args.command == "run" and args.responses:
            atomic_write(args.responses, json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
        if args.out:
            atomic_write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 3 if args.gate and not report["gate"]["passed"] else 0
    except (OSError, ValueError, TypeError, RecursionError, ImportError):
        print(json.dumps({"error": "evaluation input/configuration failed; no input content echoed"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
