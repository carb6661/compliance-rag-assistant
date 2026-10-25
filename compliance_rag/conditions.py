"""Deliberately narrow conditional-write contract; an ETag is not authorization."""
from dataclasses import dataclass
import re


ETAG = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)


class WriteRejected(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class WriteCondition:
    mode: str
    etag: str | None = None

    def __post_init__(self):
        if self.mode == "create" and self.etag is None:
            return
        if self.mode == "match" and isinstance(self.etag, str) and ETAG.fullmatch(self.etag):
            return
        raise WriteRejected("invalid_precondition", 400)

    @classmethod
    def create(cls):
        return cls("create")

    @classmethod
    def match(cls, etag):
        return cls("match", etag)


def require_condition(condition, *, delete=False):
    if condition is None:
        raise WriteRejected("precondition_required", 428)
    if not isinstance(condition, WriteCondition) or (delete and condition.mode != "match"):
        raise WriteRejected("invalid_precondition", 400)
    return condition


def from_headers(headers, *, delete=False):
    # No duplicate, comma-list, weak-tag or wildcard-If-Match interpretation.
    matches = headers.getlist("if-match")
    absent = headers.getlist("if-none-match")
    if not matches and not absent:
        raise WriteRejected("precondition_required", 428)
    if len(matches) > 1 or len(absent) > 1 or (matches and absent):
        raise WriteRejected("invalid_precondition", 400)
    if absent:
        if delete or absent[0].strip() != "*":
            raise WriteRejected("invalid_precondition", 400)
        return WriteCondition.create()
    return WriteCondition.match(matches[0].strip())
