"""Exact bounded retrieval: filtered-corpus BM25 + optional real dense cosine + RRF."""
from collections import Counter
import math
import re


STOP = set("的了是在和与及为对将把应有中个等吗呢么如何哪些什么是否需要要求可以请问说明根据进行")


def terms(text):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    result = []
    for word in words:
        if word.isascii():
            result.append(word)
        else:
            result.extend(c for c in word if c not in STOP)
            result.extend(word[i:i+2] for i in range(len(word)-1)
                          if not all(c in STOP for c in word[i:i+2]))
    return result[:8000]


def unit_vector(vector, dimension):
    values = [float(x) for x in vector]
    if len(values) != dimension or not values or not all(math.isfinite(x) for x in values):
        raise ValueError("invalid embedding shape or values")
    norm = math.sqrt(sum(x*x for x in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("zero or invalid embedding norm")
    return [x / norm for x in values]


def rank(question, rows, mode, query_vector=None, dense_min_cosine=0.50):
    """Only rows already passing mandatory SQL filters enter statistics/scoring."""
    query = set(terms(question))
    counters = [Counter(terms(row["title"] + "\n" + row["content"])) for row in rows]
    lengths = [sum(count.values()) for count in counters]
    average = sum(lengths) / len(rows) if rows else 1
    frequency = Counter(term for count in counters for term in query if term in count)
    lexical, dense = {}, {}
    for row, count, length in zip(rows, counters, lengths):
        score = 0.0
        for term in query:
            tf = count[term]
            if tf:
                idf = math.log(1 + (len(rows) - frequency[term] + 0.5)/(frequency[term] + 0.5))
                score += idf * tf * 2.2/(tf + 1.2*(0.25 + 0.75*length/max(average, 1)))
        if score > 0:
            lexical[row["chunk_id"]] = score
        if query_vector is not None:
            dense[row["chunk_id"]] = sum(a*b for a, b in zip(query_vector, row["vector"]))
    ordered = lambda scores: sorted(scores, key=lambda key: (-scores[key], key))
    lex_order, dense_order = ordered(lexical), ordered(dense)
    # Conservative lexical gate, not a calibrated relevance probability.
    eligible = {
        row["chunk_id"] for row, count in zip(rows, counters)
        if len(query & set(count)) >= min(2, len(query)) and query
    }
    semantic = {key for key, value in dense.items() if value >= dense_min_cosine}
    if mode == "bm25":
        chosen, scores = [key for key in lex_order if key in eligible], lexical
    elif mode == "dense":
        chosen, scores = [key for key in dense_order if key in semantic], dense
    else:
        scores = {}
        for ordering in (lex_order[:50], dense_order[:50]):
            for position, key in enumerate(ordering, 1):
                scores[key] = scores.get(key, 0) + 1/(60 + position)
        chosen = [key for key in ordered(scores) if key in eligible or key in semantic]
    return [{"chunk_id": key, "score": scores[key], "bm25": lexical.get(key, 0),
             "cosine": dense.get(key)} for key in chosen]
