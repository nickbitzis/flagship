"""JSON diff for the audit log: a flat list of {path, before, after} changes."""

from __future__ import annotations

_MISSING = object()


def diff(before, after, path: str = "") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after), key=str):
            out += diff(before.get(k, _MISSING), after.get(k, _MISSING), f"{path}.{k}" if path else str(k))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (b, a) in enumerate(zip(before, after, strict=True)):
            out += diff(b, a, f"{path}[{i}]")
        return out
    if before == after and type(before) is type(after):
        return []
    return [{"path": path or "$", "before": _clean(before), "after": _clean(after)}]


def _clean(v):
    return None if v is _MISSING else v
