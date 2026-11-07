"""Pure evaluation engine shared by the server and the SDK. No I/O, no clock, no globals.

A ruleset is plain JSON: {"env": "prod", "version": 12, "flags": {key: flag}, "segments": {key: segment}}.
The server stores flags with one config block per environment; `ruleset_for_env` flattens a flag
to the block for one environment so the SDK only ever sees what it needs.

Evaluation order for one flag, first match wins:
  1. kill switch or flag off          -> off variation, reason OFF
  2. prerequisites, in order          -> off variation, reason PREREQUISITE_FAILED
  3. individual targets               -> reason TARGET_MATCH
  4. rules, in order                  -> reason RULE_MATCH (with rule index and id)
  5. fallthrough                      -> reason FALLTHROUGH
Anything malformed returns the caller's default with reason ERROR, never an exception.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import operators

BUCKETS = 100_000  # weights are in thousandths of a percent, so 25% is 25000


@dataclass
class Detail:
    value: object
    variation: int | None
    reason: dict
    flag_version: int | None = None
    # Prerequisite evaluations done on the way, so the SDK can record impressions for them too.
    prereq_events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"value": self.value, "variation": self.variation, "reason": self.reason}


class EvalError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def bucket(flag_key: str, salt: str, bucket_value: str) -> int:
    """Map a user to one of 100000 buckets with sha1(flag key + salt + user key).

    The bucket depends only on those three strings, so it never changes when the rollout
    percentage changes. A user in bucket 7342 is in at 10% (bucket < 10000) and still in at 20%.
    Changing the salt reshuffles everyone, which is what you want for a fresh experiment.
    """
    digest = hashlib.sha1(f"{flag_key}.{salt}.{bucket_value}".encode()).hexdigest()
    return int(digest[:15], 16) * BUCKETS // (16**15)


def pick_weighted(weights: list[int], b: int) -> int:
    """Walk cumulative weights in variation order. Bucket b falls in exactly one slice."""
    acc = 0
    for i, w in enumerate(weights):
        acc += w
        if b < acc:
            return i
    return len(weights) - 1  # guards against weights summing a little short


def _attr(context: dict, name: str):
    if name in context:
        return context[name]
    # dotted lookup for nested attributes: "device.os"
    cur = context
    for part in name.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def clause_matches(clause: dict, context: dict, segments: dict, _depth: int = 0) -> bool:
    op = clause.get("op")
    values = clause.get("values", [])
    if op == "segment_match":
        result = any(segment_contains(segments.get(s), context, segments, _depth + 1) for s in values)
    elif op in operators.OPERATORS:
        user_value = _attr(context, clause.get("attribute", ""))
        result = user_value is not None and operators.apply(op, user_value, values)
    else:
        raise EvalError("MALFORMED_FLAG", f"unknown operator {op!r}")
    return (not result) if clause.get("negate") else result


def rule_matches(rule: dict, context: dict, segments: dict, _depth: int = 0) -> bool:
    """All clauses must match (AND). A rule with no clauses matches everyone."""
    return all(clause_matches(c, context, segments, _depth) for c in rule.get("clauses", []))


def segment_contains(segment: dict | None, context: dict, segments: dict, _depth: int = 0) -> bool:
    if segment is None:
        return False
    if _depth > 10:
        raise EvalError("MALFORMED_FLAG", "segments nested too deep")
    key = context.get("key")
    if key in segment.get("excluded", []):
        return False
    if key in segment.get("included", []):
        return True
    return any(rule_matches(r, context, segments, _depth) for r in segment.get("rules", []))


def _serve(flag: dict, serve: dict, context: dict) -> tuple[int, bool]:
    """Return (variation index, in_experiment_rollout)."""
    if "variation" in serve:
        return serve["variation"], False
    rollout = serve.get("rollout")
    if not rollout:
        raise EvalError("MALFORMED_FLAG", "serve needs a variation or a rollout")
    by = rollout.get("bucket_by", "key")
    bucket_value = _attr(context, by)
    if bucket_value is None:
        raise EvalError("USER_ATTRIBUTE_MISSING", f"rollout buckets by {by!r}")
    b = bucket(flag["key"], flag.get("salt", ""), str(bucket_value))
    return pick_weighted(rollout["weights"], b), True


def _result(flag: dict, index: int, reason: dict) -> Detail:
    variations = flag.get("variations", [])
    if not isinstance(index, int) or not 0 <= index < len(variations):
        raise EvalError("MALFORMED_FLAG", f"variation {index!r} out of range")
    return Detail(variations[index]["value"], index, reason, flag.get("version"))


def _evaluate(flag: dict, context: dict, ruleset: dict, stack: tuple, events: list) -> Detail:
    key = flag["key"]
    if key in stack:
        raise EvalError("PREREQUISITE_CYCLE", " -> ".join([*stack, key]))
    stack = (*stack, key)
    off = flag.get("off_variation")

    def off_result(reason):
        if off is None:
            return Detail(None, None, reason, flag.get("version"))
        return _result(flag, off, reason)

    if flag.get("killed"):
        return off_result({"kind": "OFF", "killed": True})
    if not flag.get("on"):
        return off_result({"kind": "OFF"})

    for prereq in flag.get("prerequisites", []):
        pre_flag = ruleset["flags"].get(prereq["key"])
        if pre_flag is None:
            return off_result({"kind": "PREREQUISITE_FAILED", "prerequisite_key": prereq["key"], "missing": True})
        pre = _evaluate(pre_flag, context, ruleset, stack, events)
        events.append((prereq["key"], pre))
        if not pre_flag.get("on") or pre_flag.get("killed") or pre.variation != prereq["variation"]:
            return off_result({"kind": "PREREQUISITE_FAILED", "prerequisite_key": prereq["key"]})

    user_key = context.get("key")
    for target in flag.get("targets", []):
        if user_key in target.get("values", []):
            return _result(flag, target["variation"], {"kind": "TARGET_MATCH"})

    segments = ruleset.get("segments", {})
    for i, rule in enumerate(flag.get("rules", [])):
        if rule_matches(rule, context, segments):
            index, rolled = _serve(flag, rule, context)
            reason = {"kind": "RULE_MATCH", "rule_index": i, "rule_id": rule.get("id")}
            if rolled:
                reason["in_rollout"] = True
            return _result(flag, index, reason)

    index, rolled = _serve(flag, flag.get("fallthrough", {}), context)
    reason = {"kind": "FALLTHROUGH"}
    if rolled:
        reason["in_rollout"] = True
    return _result(flag, index, reason)


def evaluate(ruleset: dict, flag_key: str, context: dict, default=None) -> Detail:
    """Evaluate one flag for one user. Always returns a Detail; never raises."""
    flag = ruleset.get("flags", {}).get(flag_key)
    if flag is None:
        return Detail(default, None, {"kind": "ERROR", "error_kind": "FLAG_NOT_FOUND"})
    if not isinstance(context, dict) or not context.get("key"):
        return Detail(default, None, {"kind": "ERROR", "error_kind": "USER_KEY_MISSING"})
    events: list = []
    try:
        detail = _evaluate(flag, context, ruleset, (), events)
    except EvalError as e:
        return Detail(default, None, {"kind": "ERROR", "error_kind": e.kind, "message": str(e)})
    except (KeyError, TypeError, ValueError, IndexError) as e:
        return Detail(default, None, {"kind": "ERROR", "error_kind": "MALFORMED_FLAG", "message": str(e)})
    if detail.value is None:
        detail.value = default
    detail.prereq_events = events
    return detail


def ruleset_for_env(flags: list[dict], segments: list[dict], env: str, version: int = 0) -> dict:
    """Flatten stored flags (one config block per environment) to a single-environment ruleset."""
    out = {}
    for f in flags:
        cfg = f.get("environments", {}).get(env)
        if cfg is None:
            continue
        out[f["key"]] = {
            "key": f["key"],
            "kind": f.get("kind", "boolean"),
            "variations": f["variations"],
            "salt": f.get("salt", f["key"]),
            "version": f.get("version", 0),
            "client_side": f.get("client_side", False),
            "killed": bool(f.get("killed")) or bool(cfg.get("killed")),
            "on": bool(cfg.get("on")),
            "off_variation": cfg.get("off_variation"),
            "prerequisites": cfg.get("prerequisites", []),
            "targets": cfg.get("targets", []),
            "rules": cfg.get("rules", []),
            "fallthrough": cfg.get("fallthrough", {"variation": cfg.get("off_variation", 0)}),
            "experiment": f.get("experiment"),
        }
    segs = {s["key"]: s for s in segments}
    return {"env": env, "version": version, "flags": out, "segments": segs}


def find_cycle(prereqs: dict[str, list[str]]) -> list[str] | None:
    """Depth-first search over the prerequisite graph. Returns one cycle as a key path, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {k: WHITE for k in prereqs}
    path: list[str] = []

    def visit(node):
        color[node] = GREY
        path.append(node)
        for nxt in prereqs.get(node, []):
            if color.get(nxt, WHITE) == GREY:
                return path[path.index(nxt) :] + [nxt]
            if color.get(nxt, WHITE) == WHITE and nxt in prereqs:
                found = visit(nxt)
                if found:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for k in prereqs:
        if color[k] == WHITE:
            found = visit(k)
            if found:
                return found
    return None
