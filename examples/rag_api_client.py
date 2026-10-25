"""Actual local app demo. --llm explicitly requests the server-configured external model."""
import argparse
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compliance_rag.evaluation import DATA, sample_config

parser = argparse.ArgumentParser()
parser.add_argument("--llm", action="store_true")
parser.add_argument("--retrieval", choices=["bm25","dense","hybrid"], default="bm25")
args = parser.parse_args()

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=40, trust_env=False) as client:
    for edition in ("v1","v2"):
        response = client.post("/v1/library/ingest", json={"text":(DATA/f"{edition}.md").read_bytes().decode(),
            "source_name":f"knowledge/{edition}.md", "config":sample_config(edition).model_dump()}, headers={"If-None-Match":"*"})
        if response.status_code == 412:
            # Existing demo sources may be user-edited. Never fetch a new ETag
            # and silently overwrite them just to make this demonstration green.
            raise SystemExit("Demo source already exists. Use a fresh demo database or review it with /v1/library/source; no overwrite was attempted.")
        response.raise_for_status()
    response = client.post("/v1/ask", json={"question":"进入记录保留多久？", "scope":{
        "standard_code":"TRAINING-POLICY", "edition":"v2", "security_level":"第三级", "security_class":"安全物理环境"},
        "retrieval":args.retrieval, "generation":"llm" if args.llm else "extractive"})
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
