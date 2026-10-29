"""Explicit optional, genuine ONNX integration under an outbound-socket prohibition."""
import argparse
import json
import socket
from unittest.mock import patch

from .embeddings import ChineseEmbedder
from .evaluation import evaluate
from .parser import InputError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    args = parser.parse_args()
    def forbidden(*args, **kwargs):
        raise AssertionError("outbound socket attempted during local semantic integration")
    with patch.object(socket.socket, "connect", forbidden), patch.object(socket, "getaddrinfo", forbidden):
        embedder = ChineseEmbedder(args.model_path)
        vector = embedder.query("虚构实验室进入记录")
        assert len(vector) == 512
        try:
            embedder.documents(["数据恢复核验"*600])
        except InputError:
            token_guard = True
        else:
            raise AssertionError("oversized embedding input silently truncated")
        result = evaluate(embedder)
        from .source_access_demo import semantic_check
        access = semantic_check(embedder)
    print(json.dumps({"real_embedding":True, "real_llm":False, "network_during_inference":"prohibited",
        "dimension":len(vector), "token_guard":token_guard, "embedding_identity":embedder.identity,
        "source_authorization": access,
        "questions":result["questions"],"metrics":[{k:r[k] for k in (
            "retrieval","hit_at_3","mrr_at_3","unanswerable_refusal_rate","scope_violation_hits")}
            for r in result["results"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
