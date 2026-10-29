"""Maintainer-only local client; updates require an explicitly reviewed access ETag."""
import argparse
import json
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compliance_rag.rag_cli import cli_match
from compliance_rag.conditions import WriteRejected

parser = argparse.ArgumentParser(description="Read source access, or replace its readers after reviewing the source and tag.")
parser.add_argument("source_name")
parser.add_argument("--set-readers", nargs="*", default=None, help="full replacement list; supply no IDs to revoke all query readers")
parser.add_argument("--if-match", help="reviewed access_etag, quoted or bare hex; required for changes")
args = parser.parse_args()
if (args.set_readers is not None) != (args.if_match is not None):
    parser.error("--set-readers and --if-match must be provided together")
token = os.getenv("RAG_API_TOKEN")
if not token:
    raise SystemExit("Load a maintainer RAG_API_TOKEN locally; do not put it in arguments or source code.")
payload = {"source_name": args.source_name}
headers = {"Authorization": "Bearer " + token}
route = "/v1/library/access"
if args.set_readers is not None:
    try:
        headers["If-Match"] = cli_match(args.if_match).etag
    except WriteRejected:
        parser.error("invalid access ETag")
    payload["readers"] = args.set_readers
    route += "/update"
with httpx.Client(base_url="http://127.0.0.1:8000", headers=headers, trust_env=False, timeout=30) as client:
    try:
        response = client.post(route, json=payload)
    except httpx.HTTPError:
        raise SystemExit("Local API transport failed; no automatic retry.") from None
    print(response.status_code)
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    if response.status_code != 200:
        raise SystemExit(3 if response.status_code == 412 else 2)
