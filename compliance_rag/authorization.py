"""Server-created read identities; never accepted from an HTTP request body."""
from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class ReaderGrant:
    principal_id: str
    credential_sha256: str = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.principal_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.principal_id)
                or not isinstance(self.credential_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.credential_sha256)):
            raise ValueError("invalid server-side read identity")


@dataclass(frozen=True)
class ReadIdentity(ReaderGrant):
    maintainer: bool = False

    def __post_init__(self):
        super().__post_init__()
        if type(self.maintainer) is not bool:
            raise ValueError("invalid server-side maintenance authority")


def validated_grants(grants):
    if not isinstance(grants, (list, tuple)) or len(grants) > 16 or any(type(g) is not ReaderGrant for g in grants):
        raise ValueError("invalid source reader grants")
    if len({g.principal_id for g in grants}) != len(grants):
        raise ValueError("duplicate source reader")
    return sorted(grants, key=lambda g: g.principal_id)
