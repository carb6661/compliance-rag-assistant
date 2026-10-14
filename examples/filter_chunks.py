"""Exact metadata filter over locally generated JSONL; not semantic retrieval."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("input", type=Path)
parser.add_argument("--level", required=True)
parser.add_argument("--security-class", required=True)
args = parser.parse_args()
matches = []
for line in args.input.read_text(encoding="utf-8").splitlines():
    doc = json.loads(line)
    m = doc["metadata"]
    if m["安全级别"] == args.level and m["安全类"] == args.security_class:
        matches.append({"source":m["来源文件"], "lines":[m["起始行"],m["结束行"]],
                        "chunk_id":m["块ID"], "text":doc["page_content"]})
print(json.dumps({"matches":len(matches), "documents":matches}, ensure_ascii=False, indent=2))
