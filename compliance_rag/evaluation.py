"""Small declared synthetic evaluation; never reports mock LLMs as real generation."""
import json
import math
import struct
from pathlib import Path
import tempfile
import time

from .generation import AskRequest, RagService
from .knowledge import KnowledgeStore, Scope
from .models import IngestConfig
from .parser import normalize_text
from .retrieval import rank, unit_vector
from .conditions import WriteCondition

DATA = Path(__file__).resolve().parent.parent / "examples/knowledge"


def sample_config(edition):
    return IngestConfig.model_validate({"standards": {"虚构组织安全操作手册": {
        "name": "虚构组织安全操作手册", "code": "TRAINING-POLICY", "edition": edition, "synthetic": True}},
        "max_chars": 300})


def seed(store):
    for edition in ("v1", "v2"):
        text = (DATA/f"{edition}.md").read_bytes().decode("utf-8")
        store.ingest(text, f"knowledge/{edition}.md", sample_config(edition), condition=WriteCondition.create())


def evaluate(embedder=None, dense_min_cosine=0.50):
    questions = json.loads((DATA/"questions.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="compliance-rag-eval-") as directory:
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3", embedder, dense_min_cosine)
        seed(store)
        with store.connect() as db:
            raw = [dict(row) for row in db.execute("SELECT * FROM chunks ORDER BY chunk_id")]
        modes = ["bm25"] + (["dense", "hybrid"] if embedder else [])
        reports = []
        for mode in modes:
            results = []
            for item in questions:
                request = AskRequest(question=item["question"], scope=Scope(
                    standard_code="TRAINING-POLICY", edition=item["edition"], security_level=item["level"],
                    security_class=item["class"]), retrieval=mode)
                started = time.perf_counter()
                response = RagService(store).ask(request)
                elapsed = (time.perf_counter()-started)*1000
                hits = response["retrieval"]["hits"]
                relevant = [i for i, hit in enumerate(hits, 1) if item["expected"] and item["expected"] in hit["evidence_text"]]
                citations_valid = all(
                    normalize_text((DATA/Path(c["source"]).name).read_bytes().decode("utf-8"))[c["start_char"]:c["end_char"]] == c["quote"]
                    for c in response["citations"])
                results.append({"id": item["id"], "expected_answerable": item["expected"] is not None,
                    "hit_at_3": bool(relevant), "reciprocal_rank": 1/min(relevant) if relevant else 0,
                    "status": response["status"], "reason": response["reason"], "citation_spans_valid": citations_valid,
                    "scope_violations": sum(h["metadata"]["标准版本"] != item["edition"] or h["metadata"]["安全级别"] != item["level"] for h in hits),
                    "latency_ms": round(elapsed, 3)})
            answerable = [r for r in results if r["expected_answerable"]]
            unanswerable = [r for r in results if not r["expected_answerable"]]
            latency = sorted(r["latency_ms"] for r in results)
            reports.append({"retrieval": mode, "answerable_questions": len(answerable),
                "hit_at_3": sum(r["hit_at_3"] for r in answerable)/len(answerable),
                "mrr_at_3": sum(r["reciprocal_rank"] for r in answerable)/len(answerable),
                "unanswerable_refusal_rate": sum(r["status"] == "refused" for r in unanswerable)/len(unanswerable),
                "scope_violation_hits": sum(r["scope_violations"] for r in results),
                "p95_local_ms": latency[math.ceil(len(latency)*0.95)-1], "cases": results})
        # Deliberately naive, read-only baseline: same BM25, no scope filter.
        blind_violations = 0
        lookup = {r["chunk_id"]: r for r in raw}
        for item in questions:
            if item["expected"] is None:
                continue
            for hit in rank(item["question"], raw, "bm25")[:3]:
                row = lookup[hit["chunk_id"]]
                blind_violations += row["edition"] != item["edition"] or row["level"] != item["level"]
        probes = []
        if embedder:
            for item in questions:
                subset = [row for row in raw if row["edition"] == item["edition"] and row["level"] == item["level"]
                          and (item["class"] is None or row["security_class"] == item["class"])]
                query_vector = unit_vector(embedder.query(item["question"]), embedder.dimension)
                scores = sorted([(sum(a*b for a,b in zip(query_vector, struct.unpack(f"<{embedder.dimension}f", row["vector"]))), row)
                                 for row in subset], key=lambda pair: -pair[0])
                probes.append({"id": item["id"], "best_cosine": round(scores[0][0], 6) if scores else None,
                    "ungated_gold_rank": next((i for i, (_, row) in enumerate(scores, 1)
                         if item["expected"] and item["expected"] in row["content"]), None)})
        return {"dataset": "original-fictional-policy-20q-v1", "documents": 2, "chunks": len(raw),
            "questions": len(questions), "generation": "extractive-not-real-llm", "dense_min_cosine": dense_min_cosine,
            "embedding_identity": store.identity, "unfiltered_bm25_scope_violation_hits": blind_violations,
            "results": reports, "dense_probes": probes, "limitations": "20 authored questions on 12 small synthetic chunks, no held-out or production accuracy claim. No real LLM generation evaluated. Related-but-unsupported question intentionally exposes refusal weakness; P95 is a small-sample nearest-rank observation, not an SLA."}
