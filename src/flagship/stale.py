"""Stale flag detection, pure functions over an export of flags plus evaluation stats.

A flag is stale in an environment when it has served one constant value for more than N days:
on with every path serving the same variation (a finished 100% rollout), or off (0%). A flag is
also stale when nothing has evaluated it in any environment for N days since it was created.
"""

from __future__ import annotations

DAY = 86_400


def reachable_variations(flag: dict, env: str) -> set[int]:
    cfg = flag.get("environments", {}).get(env)
    if cfg is None:
        return set()
    off = cfg.get("off_variation")
    if flag.get("killed") or cfg.get("killed") or not cfg.get("on"):
        return {off} if off is not None else set()
    out: set[int] = set()
    if cfg.get("prerequisites") and off is not None:
        out.add(off)
    for t in cfg.get("targets", []):
        if t.get("values"):
            out.add(t["variation"])
    for serve in [*cfg.get("rules", []), cfg.get("fallthrough", {})]:
        if "variation" in serve:
            out.add(serve["variation"])
        elif "rollout" in serve:
            out.update(i for i, w in enumerate(serve["rollout"].get("weights", [])) if w > 0)
    return out


def find_stale(export: dict, now: float, days: int = 30, env: str = "prod") -> list[dict]:
    """export = {"flags": [...], "eval_stats": {flag_key: {"count": n, "last_ts": t}}}."""
    stats = export.get("eval_stats", {})
    out = []
    for flag in export.get("flags", []):
        key = flag["key"]
        if "permanent" in flag.get("tags", []):
            continue  # long-lived operational flags (kill switches, config) are meant to stay
        changed = flag.get("updated_at", flag.get("created_at", now))
        created = flag.get("created_at", changed)
        reach = reachable_variations(flag, env)
        if len(reach) == 1 and now - changed > days * DAY:
            idx = next(iter(reach))
            cfg = flag["environments"][env]
            state = "on" if cfg.get("on") and not flag.get("killed") else "off"
            out.append(
                {
                    "key": key,
                    "kind": "constant",
                    "detail": f"{env} {state}, serves {flag['variations'][idx]['value']!r} to everyone",
                    "value": flag["variations"][idx]["value"],
                    "days": int((now - changed) // DAY),
                }
            )
            continue
        st = stats.get(key)
        last = st.get("last_ts") if st else None
        if last is None and now - created > days * DAY:
            out.append(
                {
                    "key": key,
                    "kind": "never_evaluated",
                    "detail": "no evaluations in any environment",
                    "value": None,
                    "days": int((now - created) // DAY),
                }
            )
        elif last is not None and now - last > days * DAY:
            out.append(
                {
                    "key": key,
                    "kind": "not_evaluated_recently",
                    "detail": f"last evaluated {int((now - last) // DAY)} days ago",
                    "value": None,
                    "days": int((now - last) // DAY),
                }
            )
    return out
