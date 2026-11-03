"""Fixed private subprocess entry point. Binary JSON stdin/stdout; no DB or tool execution."""
import json
from pathlib import Path
import sys

# The parent uses Python -I; add only this trusted package location explicitly.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compliance_rag.generation import CompatibleGenerator, CompatibleSettings, ModelFailure, Selection, strict_json
from compliance_rag.isolated_generation import MAX_PACKET, UPSTREAM_REASONS, WorkerRequest


def run(raw):
    try:
        if len(raw) > MAX_PACKET:
            raise ValueError()
        request = WorkerRequest.model_validate(strict_json(raw.decode("utf-8")))
    except (ValueError, TypeError, RecursionError):
        return {"protocol": 1, "status": "error", "reason": "invalid_model_selection"}
    try:
        settings = CompatibleSettings(request.provider.base_url, request.provider.model, request.provider.api_key)
        selected = CompatibleGenerator(settings).select(request.question,
            [{"citation_id": item.citation_id, "evidence_text": item.text} for item in request.evidence])
        selection = Selection.model_validate(selected)
        return {"protocol": 1, "status": "ok", "selection": selection.model_dump()}
    except ModelFailure as error:
        return {"protocol": 1, "status": "error", "reason": str(error) if str(error) in UPSTREAM_REASONS else "invalid_model_selection"}
    except (ValueError, TypeError, RecursionError):
        return {"protocol": 1, "status": "error", "reason": "invalid_model_selection"}


def main():
    try:
        result = run(sys.stdin.buffer.read(MAX_PACKET+1))
        packet = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(packet) > MAX_PACKET:
            raise ValueError()
        sys.stdout.buffer.write(packet)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # No raw exception, credential, question, source or provider body on either pipe.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
