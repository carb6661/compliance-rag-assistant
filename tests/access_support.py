"""Explicit test-only credential. Never a deployment default."""
import hashlib
from compliance_rag.access import AccessPolicy, AccessSettings

TOKEN = "synthetic_test_token_" + "a" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}


def make_policy():
    return AccessPolicy(AccessSettings(principals=[{"id": "test", "role": "maintainer",
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()}],
        limits={"requests_per_minute": 6000, "active_per_principal": 8}))
