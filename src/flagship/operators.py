"""Clause operators. Each takes the user's attribute value and one clause value and returns a bool.

A clause matches when the operator is true for any of the clause's values (OR). If the user's
attribute is a list, the clause matches when any element matches. Type mismatches never raise,
they just do not match, so a bad user attribute can never crash an evaluation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import lru_cache

_SEMVER = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)(?:\.(?P<minor>0|[1-9]\d*))?(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def parse_semver(value) -> tuple | None:
    """Parse "2.4.1", "2.4" or "v3.0.0-beta.2" into a sortable tuple. Missing parts count as 0."""
    if not isinstance(value, str):
        return None
    m = _SEMVER.match(value.strip())
    if not m:
        return None
    core = (int(m["major"]), int(m["minor"] or 0), int(m["patch"] or 0))
    pre = m["pre"]
    if pre is None:
        # A release sorts after any pre-release of the same version.
        return (*core, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (*core, 0, parts)


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def parse_time(v) -> float | None:
    """Accept epoch milliseconds or an ISO 8601 string. Returns epoch seconds."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v) / 1000.0
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


@lru_cache(maxsize=1024)
def _regex(pattern: str):
    return re.compile(pattern)


def _cmp(fn, a, b, parse):
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return False
    return fn(pa, pb)


def _between(user, bounds):
    if not isinstance(bounds, list | tuple) or len(bounds) != 2:
        return False
    x, lo, hi = _num(user), _num(bounds[0]), _num(bounds[1])
    if x is None or lo is None or hi is None:
        return False
    return lo <= x < hi


def _matches(user, pattern):
    if not isinstance(user, str) or not isinstance(pattern, str):
        return False
    try:
        return _regex(pattern).search(user) is not None
    except re.error:
        return False


def _equals(u, v):
    nu, nv = _num(u) if not isinstance(u, str) else None, _num(v) if not isinstance(v, str) else None
    if nu is not None and nv is not None:
        return nu == nv
    return type(u) is type(v) and u == v


OPERATORS = {
    "equals": _equals,
    "in": _equals,  # same test; by convention an "in" clause lists several values
    "contains": lambda u, v: isinstance(u, str) and isinstance(v, str) and v in u,
    "starts_with": lambda u, v: isinstance(u, str) and isinstance(v, str) and u.startswith(v),
    "ends_with": lambda u, v: isinstance(u, str) and isinstance(v, str) and u.endswith(v),
    "matches": _matches,
    "lt": lambda u, v: _cmp(lambda a, b: a < b, u, v, _num),
    "lte": lambda u, v: _cmp(lambda a, b: a <= b, u, v, _num),
    "gt": lambda u, v: _cmp(lambda a, b: a > b, u, v, _num),
    "gte": lambda u, v: _cmp(lambda a, b: a >= b, u, v, _num),
    "between": _between,  # value is [low, high), half open
    "semver_eq": lambda u, v: _cmp(lambda a, b: a == b, u, v, parse_semver),
    "semver_lt": lambda u, v: _cmp(lambda a, b: a < b, u, v, parse_semver),
    "semver_gt": lambda u, v: _cmp(lambda a, b: a > b, u, v, parse_semver),
    "before": lambda u, v: _cmp(lambda a, b: a < b, u, v, parse_time),
    "after": lambda u, v: _cmp(lambda a, b: a > b, u, v, parse_time),
}

# segment_match is handled by the engine because it needs the segment table.
ALL_OPS = sorted([*OPERATORS, "segment_match"])


def apply(op: str, user_value, clause_values: list) -> bool:
    fn = OPERATORS[op]
    candidates = user_value if isinstance(user_value, list) else [user_value]
    return any(fn(u, v) for u in candidates for v in clause_values)
