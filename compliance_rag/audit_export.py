"""Bounded, read-only loopback export. Raw audit metadata stays in an explicit local file."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import httpx

from .__main__ import atomic_write, check_outputs
from .generation import strict_json


def collect(client, *, page_size=100):
    if type(page_size) is not int or page_size not in (50, 100):
        raise ValueError("unsupported export page size")
    events, snapshot, cursor, last = [], None, None, 0
    for _ in range(10_000 // page_size):
        # Default 100 needs <=100 requests, below the server's default 120/min quota.
        payload = {"limit": page_size, "cursor": cursor}
        with client.stream("POST", "/v1/audit/events", json=payload) as response:
            if response.status_code != 200:
                raise ValueError("audit export HTTP failure")
            raw = bytearray()
            for block in response.iter_bytes(8192):
                raw.extend(block)
                if len(raw) > 524288:
                    raise ValueError("audit page byte budget exceeded")
        page = strict_json(raw.decode("utf-8"))
        if not isinstance(page, dict) or set(page) != {"snapshot", "events", "next_cursor"}:
            raise ValueError("invalid audit page")
        current = page["snapshot"]
        if (not isinstance(current, dict) or set(current) != {"instance_id", "through_seq", "audit_start_revision"}
                or not isinstance(current["instance_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", current["instance_id"])
                or type(current["through_seq"]) is not int or not 0 <= current["through_seq"] <= 2**63-1
                or type(current["audit_start_revision"]) is not int or current["audit_start_revision"] < 0
                or snapshot is not None and current != snapshot):
            raise ValueError("audit export snapshot changed")
        snapshot = current
        rows = page["events"]
        if not isinstance(rows, list) or len(rows) > page_size:
            raise ValueError("invalid audit row count")
        for event in rows:
            if (not isinstance(event, dict) or type(event.get("seq")) is not int
                    or not last < event["seq"] <= snapshot["through_seq"]):
                raise ValueError("non-increasing or out-of-snapshot audit sequence")
            last = event["seq"]
            events.append(event)
        cursor = page["next_cursor"]
        if cursor is None:
            if last != snapshot["through_seq"]:
                raise ValueError("audit export ended before snapshot ceiling")
            return {"format": "private-maintenance-export-v1", "snapshot": snapshot, "events": events}
        expected = {"instance_id": snapshot["instance_id"], "after_seq": last, "through_seq": snapshot["through_seq"]}
        if not rows or cursor != expected:
            raise ValueError("audit export cursor failed to advance")
    raise ValueError("audit export page budget exceeded")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read a fixed private maintenance snapshot from an authorized local API")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    try:
        url = urlsplit(args.url)
        _ = url.port
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("loopback API origin required")
        credential = os.environ.get("RAG_API_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", credential):
            raise ValueError("maintainer client credential required")
        check_outputs([args.out], set(), False)
        with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer "+credential},
                          timeout=10, trust_env=False, follow_redirects=False) as client:
            export = collect(client)
        atomic_write(args.out, json.dumps(export, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps({"status": "exported", "events": len(export["events"])}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, httpx.HTTPError):
        print(json.dumps({"error": "audit export failed; no partial file or input details published"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
