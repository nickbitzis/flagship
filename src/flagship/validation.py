"""Write-time validation for flags and segments. Returns a list of human readable problems."""

from __future__ import annotations

import re

from . import engine, operators

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
KINDS = {"boolean", "string", "number", "json"}


def _check_serve(serve, n_variations: int, where: str, errors: list) -> None:
    if not isinstance(serve, dict):
        errors.append(f"{where}: serve must be an object")
        return
    if "variation" in serve:
        v = serve["variation"]
        if not isinstance(v, int) or not 0 <= v < n_variations:
            errors.append(f"{where}: variation {v!r} out of range")
        return
    rollout = serve.get("rollout")
    if not isinstance(rollout, dict) or not isinstance(rollout.get("weights"), list):
        errors.append(f"{where}: needs a variation or a rollout with weights")
        return
    w = rollout["weights"]
    if len(w) != n_variations:
        errors.append(f"{where}: rollout has {len(w)} weights for {n_variations} variations")
    if any(not isinstance(x, int) or x < 0 for x in w):
        errors.append(f"{where}: weights must be non-negative integers")
    elif sum(w) != engine.BUCKETS:
        errors.append(f"{where}: weights sum to {sum(w)}, expected {engine.BUCKETS}")


def _check_clauses(clauses, where: str, errors: list, segment_keys: set | None) -> None:
    if not isinstance(clauses, list):
        errors.append(f"{where}: clauses must be a list")
        return
    for j, c in enumerate(clauses):
        cw = f"{where} clause {j}"
        op = c.get("op") if isinstance(c, dict) else None
        if op not in operators.ALL_OPS:
            errors.append(f"{cw}: unknown operator {op!r}")
            continue
        if not isinstance(c.get("values"), list) or not c["values"]:
            errors.append(f"{cw}: values must be a non-empty list")
            continue
        if op == "segment_match":
            if segment_keys is not None:
                for s in c["values"]:
                    if s not in segment_keys:
                        errors.append(f"{cw}: unknown segment {s!r}")
            continue
        if not c.get("attribute"):
            errors.append(f"{cw}: attribute is required")
        if op == "matches":
            for v in c["values"]:
                try:
                    re.compile(v)
                except (re.error, TypeError):
                    errors.append(f"{cw}: bad regex {v!r}")
        if op.startswith("semver"):
            for v in c["values"]:
                if operators.parse_semver(v) is None:
                    errors.append(f"{cw}: {v!r} is not a semantic version")
        if op in ("before", "after"):
            for v in c["values"]:
                if operators.parse_time(v) is None:
                    errors.append(f"{cw}: {v!r} is not a date")
        if op == "between":
            for v in c["values"]:
                if not (isinstance(v, list) and len(v) == 2):
                    errors.append(f"{cw}: between takes [low, high] pairs")


def validate_flag(flag: dict, envs: list[str], segment_keys: set | None = None) -> list[str]:
    errors: list[str] = []
    key = flag.get("key", "")
    if not isinstance(key, str) or not KEY_RE.match(key):
        errors.append("key must be 2 to 64 chars of a-z, 0-9, dot, dash, underscore")
    kind = flag.get("kind")
    if kind not in KINDS:
        errors.append(f"kind must be one of {sorted(KINDS)}")
    variations = flag.get("variations")
    if not isinstance(variations, list) or len(variations) < 2:
        errors.append("a flag needs at least 2 variations")
        return errors
    n = len(variations)
    values = [v.get("value") if isinstance(v, dict) else None for v in variations]
    type_ok = {
        "boolean": lambda x: isinstance(x, bool),
        "string": lambda x: isinstance(x, str),
        "number": lambda x: isinstance(x, int | float) and not isinstance(x, bool),
        "json": lambda x: isinstance(x, dict | list),
    }.get(kind, lambda x: True)
    for i, v in enumerate(values):
        if not type_ok(v):
            errors.append(f"variation {i}: value {v!r} does not match kind {kind}")
    if kind == "boolean" and sorted(values, key=str) != [False, True]:
        errors.append("a boolean flag has exactly the variations true and false")
    exp = flag.get("experiment")
    if exp is not None:
        if not isinstance(exp, dict) or not exp.get("primary_metric"):
            errors.append("experiment needs a primary_metric")
        elif not isinstance(exp.get("control", 0), int) or not 0 <= exp.get("control", 0) < n:
            errors.append("experiment control variation out of range")
    envs_cfg = flag.get("environments")
    if not isinstance(envs_cfg, dict):
        errors.append("environments must be an object")
        return errors
    for env, cfg in envs_cfg.items():
        w = f"{env}"
        if env not in envs:
            errors.append(f"unknown environment {env!r}")
            continue
        off = cfg.get("off_variation")
        if off is not None and (not isinstance(off, int) or not 0 <= off < n):
            errors.append(f"{w}: off_variation out of range")
        for i, rule in enumerate(cfg.get("rules", [])):
            _check_clauses(rule.get("clauses", []), f"{w} rule {i}", errors, segment_keys)
            _check_serve(rule, n, f"{w} rule {i}", errors)
        _check_serve(cfg.get("fallthrough", {}), n, f"{w} fallthrough", errors)
        for t in cfg.get("targets", []):
            if not isinstance(t.get("variation"), int) or not 0 <= t["variation"] < n:
                errors.append(f"{w}: target variation out of range")
        for p in cfg.get("prerequisites", []):
            if p.get("key") == key:
                errors.append(f"{w}: a flag cannot be its own prerequisite")
    return errors


def validate_segment(seg: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(seg.get("key"), str) or not KEY_RE.match(seg["key"]):
        errors.append("key must be 2 to 64 chars of a-z, 0-9, dot, dash, underscore")
    for field in ("included", "excluded"):
        if not isinstance(seg.get(field, []), list):
            errors.append(f"{field} must be a list of user keys")
    for i, rule in enumerate(seg.get("rules", [])):
        _check_clauses(rule.get("clauses", []), f"segment rule {i}", errors, None)
    return errors


def prerequisite_cycle(flags: list[dict], env: str) -> list[str] | None:
    graph = {
        f["key"]: [p["key"] for p in f.get("environments", {}).get(env, {}).get("prerequisites", [])] for f in flags
    }
    return engine.find_cycle(graph)
