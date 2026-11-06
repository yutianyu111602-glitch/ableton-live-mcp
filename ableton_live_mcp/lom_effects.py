"""Derive effects from the pinned catalogue, never from a caller-supplied flag."""

from functools import lru_cache

from .lom_catalog import LomCatalog


@lru_cache(maxsize=1)
def _catalog():
    return LomCatalog()


def is_mutating_request(payload):
    if payload.get("op") == "set":
        return True
    if payload.get("op") != "call":
        return False
    try:
        member = _catalog().validate_operation(
            payload.get("object_type"), payload.get("member"), "call"
        )
        return bool(member.get("mutating", True))
    except ValueError:
        return True


def may_write(payload):
    return is_mutating_request(payload) and not payload.get("dry_run", True)
