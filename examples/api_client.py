"""Run from the repository root; sends only the checked-in synthetic sample."""
from pathlib import Path
import httpx

response = httpx.post(
    "http://127.0.0.1:8000/v1/parse",
    json={"source_name": "synthetic-standard.md",
          "text": Path("examples/standards/synthetic-standard.md").read_text(encoding="utf-8")},
    timeout=30,
    trust_env=False,
)
response.raise_for_status()
data = response.json()
print(data["status"], data["accepted_chunks"])
for doc in data["documents"]:
    m = doc["metadata"]
    print(m["安全级别"], m["安全类"], m["条款标题"], m["起始行"], m["结束行"])
