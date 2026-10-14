"""Batch ingestion: explicit partial mode, protected outputs, no remote fetching."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from pydantic import ValidationError
from .models import IngestConfig, MAX_FILES, MAX_CHUNKS
from .parser import InputError, canonical_json, parse_file


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON configuration key")
        result[key] = value
    return result


def load_config(path):
    if path is None:
        return IngestConfig()
    with Path(path).open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise InputError("config exceeds 64 KiB")
    return IngestConfig.model_validate(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object))


def discover(source):
    source = Path(source)
    if source.is_symlink():
        raise InputError("source symlink not allowed")
    if source.is_file():
        if source.suffix.lower() != ".md":
            raise InputError("input extension must be .md")
        return [(source, source.name)]
    if not source.is_dir():
        raise InputError("input does not exist")
    base = source.resolve()
    files = []
    for path in sorted(source.rglob("*")):
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise InputError("symlink or out-of-root Markdown detected")
        files.append((path, path.relative_to(source).as_posix()))
        if len(files) > MAX_FILES:
            raise InputError("batch exceeds 32 Markdown files")
    if not files:
        raise InputError("no Markdown files")
    return files


def check_outputs(paths, protected, force):
    resolved = [Path(p).resolve() for p in paths if p]
    if len(resolved) != len(set(resolved)) or any(p in protected for p in resolved):
        raise InputError("output collides with input/config/another output")
    for raw in paths:
        if raw and (Path(raw).is_symlink() or (Path(raw).exists() and not force)):
            raise InputError("output exists or is a symlink; choose a new path or --force")


def atomic_write(path, text, force=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".ingest-", delete=False) as stream:
            temporary = stream.name
            stream.write(text)
        if force:
            os.replace(temporary, target)
        else:
            # Atomic create-without-replace; another writer cannot be overwritten.
            os.link(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="结构化合规Markdown入库；无大模型、无网络请求。")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("parse")
    ingest.add_argument("source", help="Markdown file or directory (recursive, at most 32 files)")
    ingest.add_argument("--config")
    ingest.add_argument("--out", help="JSONL output; omit for validation only")
    ingest.add_argument("--diagnostics", help="JSON diagnostics and summary")
    ingest.add_argument("--max-chars", type=int)
    ingest.add_argument("--allow-partial", action="store_true")
    ingest.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.max_chars is not None:
            config = IngestConfig.model_validate({**config.model_dump(), "max_chars": args.max_chars})
        files = discover(args.source)
        protected = {path.resolve() for path, _ in files}
        if args.config:
            protected.add(Path(args.config).resolve())
        check_outputs([args.out, args.diagnostics], protected, args.force)
        docs, diagnostics, sources = [], [], []
        for path, name in files:
            result = parse_file(path, name, config)
            docs.extend(result.documents)
            if len(docs) > MAX_CHUNKS:
                raise InputError("batch exceeds 10000 chunks")
            diagnostics.extend(d.model_dump() for d in result.diagnostics)
            sources.append({"source": name, "sha256": result.source_hash})
        errors = sum(d["severity"] == "error" for d in diagnostics)
        blocked = bool(errors and not args.allow_partial)
        exported = [] if blocked else docs
        report = {"status": "blocked" if blocked else ("partial" if errors else "success"),
                  "files": len(files), "candidate_chunks": len(docs), "accepted_chunks": len(exported),
                  "errors": errors, "warnings": sum(d["severity"] == "warning" for d in diagnostics),
                  "sources": sources, "diagnostics": diagnostics,
                  "mode": "export" if args.out else "check"}
        if args.diagnostics:
            atomic_write(args.diagnostics, json.dumps(report, ensure_ascii=False, indent=2) + "\n", args.force)
        if args.out and not blocked:
            atomic_write(args.out, "".join(canonical_json({"page_content": d.page_content,
                                                         "metadata": d.metadata}) + "\n" for d in exported), args.force)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if blocked else (1 if errors else 0)
    except (InputError, OSError, UnicodeError, ValueError, ValidationError, RecursionError):
        print("入库未完成：检查文件、配置、UTF-8、资源上限及输出冲突。输出为文件级原子写入，请检查已有输出状态。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
