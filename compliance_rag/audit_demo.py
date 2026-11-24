"""Real SQLite and ASGI maintenance requests on synthetic material; no LLM."""
import hashlib
import json
from pathlib import Path
import secrets
import tempfile

from fastapi.testclient import TestClient

from .access import AccessPolicy, AccessSettings
from .api import create_app
from .evaluation import DATA, sample_config
from .generation import RagService
from .knowledge import KnowledgeStore


def demonstrate():
    tokens = {role: secrets.token_urlsafe(32) for role in ("reader", "maintainer")}
    policy = AccessPolicy(AccessSettings(principals=[{"id": "synthetic-"+role, "role": role,
        "token_sha256": hashlib.sha256(value.encode()).hexdigest()} for role, value in tokens.items()]))
    headers = {"Authorization": "Bearer " + tokens["maintainer"]}
    text = (DATA/"v2.md").read_text(encoding="utf-8")
    name = "synthetic/audit-policy.md"
    with tempfile.TemporaryDirectory(prefix="rag-maintenance-audit-") as directory:
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3")
        app = create_app(policy)
        app.state.rag_service = RagService(store)
        with TestClient(app) as client:
            payload = {"text": text, "source_name": name, "config": sample_config("v2").model_dump()}
            created = client.post("/v1/library/ingest", json=payload, headers={**headers, "If-None-Match": "*"})
            assert created.status_code == 200
            original_tag = created.json()["source_etag"]
            access = store.source_access(name)
            granted = client.post("/v1/library/access/update", json={"source_name": name, "readers": ["synthetic-reader"]},
                                  headers={**headers, "If-Match": access["access_etag"]})
            assert granted.status_code == 200
            first = client.post("/v1/audit/events", json={"limit": 1}, headers=headers).json()
            payload["text"] = text.replace("180天", "181天")
            replaced = client.post("/v1/library/ingest", json=payload, headers={**headers, "If-Match": original_tag})
            assert replaced.status_code == 200 and replaced.json()["reader_grants_cleared"] == 1
            continuation = client.post("/v1/audit/events", json={"cursor": first["next_cursor"]}, headers=headers).json()
            assert [e["seq"] for e in first["events"]+continuation["events"]] == [1, 2]
            assert continuation["next_cursor"] is None
            denied = [client.post("/v1/audit/events", json={}).status_code,
                client.post("/v1/audit/events", json={}, headers={"Authorization":"Bearer "+tokens["reader"]}).status_code]
            stale = client.post("/v1/library/remove", json={"source_name": name}, headers={**headers, "If-Match": original_tag})
            assert stale.status_code == 412
            new_tag = replaced.json()["source_etag"]
            noop = client.post("/v1/library/ingest", json=payload, headers={**headers, "If-Match": new_tag})
            assert noop.json()["status"] == "unchanged" and len(store.audit_page()["events"]) == 3
            # Inject an actual SQLite audit-insert error after the source deletion SQL.
            with store.connect() as db:
                db.execute("CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON maintenance_events "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic audit write failed'); END")
            failed = client.post("/v1/library/remove", json={"source_name": name}, headers={**headers, "If-Match": new_tag})
            assert failed.status_code == 503
            assert store.source(name)["source_etag"] == new_tag and len(store.audit_page()["events"]) == 3
            with store.connect() as db:
                db.execute("DROP TRIGGER synthetic_audit_failure")
            removed = client.post("/v1/library/remove", json={"source_name": name}, headers={**headers, "If-Match": new_tag})
            assert removed.status_code == 200
        history = KnowledgeStore(store.path).audit_page()["events"]
        assert store.source(name) is None and len(history) == 4
        assert all(e["actor_kind"] == "api" and e["actor_id"] == "synthetic-maintainer" for e in history)
        encoded = json.dumps(history, ensure_ascii=False)
        assert "180天" not in encoded and "181天" not in encoded and all(token not in encoded for token in tokens.values())
        return {"format": "maintenance-audit-demo-v1", "transport": "in-process-asgi+real-sqlite", "real_llm": False,
            "data": "original-synthetic-policy", "events_after_delete_and_restart": len(history),
            "actions": [e["action"] for e in history], "revisions": [e["revision"] for e in history],
            "api_actor_from_authenticated_lease": True, "replacement_reader_grants_cleared": 1,
            "audit_read_statuses": denied+[200], "stale_write_status": stale.status_code,
            "unchanged_write_added_events": 0, "audit_insert_failure_status": failed.status_code,
            "audit_failure_source_and_history_unchanged": True,
            "paginated_snapshot_sequences": [1, 2], "later_commit_excluded_from_snapshot": True,
            "raw_text_and_tokens_in_event": False,
            "limits": "Committed maintenance changes only; no rejected-attempt log, IdP, WORM or response-delivery guarantee."}


if __name__ == "__main__":
    print(json.dumps(demonstrate(), ensure_ascii=False, indent=2))
