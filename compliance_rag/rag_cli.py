"""Phase-two application CLI, separate from the backward-compatible parse command."""
import argparse
import json
from pathlib import Path
import sys

from .__main__ import atomic_write, load_config
from .generation import AskRequest, RagService, generator_from_env
from .knowledge import KnowledgeStore
from .parser import InputError


def main(argv=None):
    parser = argparse.ArgumentParser(description="持久化合规证据问答；LLM调用需服务端环境变量显式许可")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-model")
    prepare.add_argument("--cache-dir", default=".models")
    for name in ("ingest", "ask", "inventory", "remove", "evaluate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", default="output/knowledge.sqlite3")
        cmd.add_argument("--model-path", help="Prepared local BGE model directory; no auto download")
        if name == "ingest":
            cmd.add_argument("source")
            cmd.add_argument("--source-name")
            cmd.add_argument("--config")
        if name == "remove":
            cmd.add_argument("source_name")
        if name == "ask":
            cmd.add_argument("question")
            cmd.add_argument("--standard-code", required=True)
            cmd.add_argument("--edition", required=True)
            cmd.add_argument("--level", required=True)
            cmd.add_argument("--security-class")
            cmd.add_argument("--retrieval", choices=["bm25", "dense", "hybrid"], default="bm25")
            cmd.add_argument("--generation", choices=["extractive", "llm"], default="extractive")
        if name == "evaluate":
            cmd.add_argument("--out")
            cmd.add_argument("--dense-min-cosine", type=float, default=0.50)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-model":
            from .embeddings import prepare_model
            print(json.dumps({"model_path": prepare_model(args.cache_dir)}))
            return 0
        embedder = None
        if args.model_path:
            from .embeddings import ChineseEmbedder
            embedder = ChineseEmbedder(args.model_path)
        if args.command == "evaluate":
            from .evaluation import evaluate
            result = evaluate(embedder, args.dense_min_cosine)
            if args.out:
                atomic_write(args.out, json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        else:
            store = KnowledgeStore(args.db, embedder)
            if args.command == "ingest":
                with Path(args.source).open("rb") as stream:
                    raw = stream.read(1048577)
                if len(raw) > 1048576:
                    raise InputError("source exceeds byte budget")
                result = store.ingest(raw.decode("utf-8"), args.source_name or Path(args.source).name, load_config(args.config))
            elif args.command == "inventory":
                result = store.inventory()
            elif args.command == "remove":
                result = store.delete(args.source_name)
            else:
                request = AskRequest(question=args.question, scope={"standard_code": args.standard_code,
                    "edition": args.edition, "security_level": args.level, "security_class": args.security_class},
                    retrieval=args.retrieval, generation=args.generation)
                result = RagService(store, generator_from_env()).ask(request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") == "refused" else 0
    except (OSError, InputError, ValueError, ImportError, RecursionError):
        print(json.dumps({"error": "operation failed: check input, prepared model, index identity and private configuration"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
