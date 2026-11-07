"""Synthetic demo data for a made-up online store, Larkspur Supply Co.

Eleven flags covering every kind and most operators, three segments, a rollout history in the
audit log, a pending four-eyes approval, a scheduled change, and a simulated A/B test on the new
checkout with 20,000 users.
"""

from __future__ import annotations

import copy
import random
import time

from . import engine
from .store import Store

DAY = 86_400
BOOL = [{"value": True, "name": "on"}, {"value": False, "name": "off"}]


def _rollout(pct_true: float) -> dict:
    t = round(pct_true * 1000)
    return {"rollout": {"weights": [t, engine.BUCKETS - t], "bucket_by": "key"}}


def _envs(prod: dict, staging: dict | None = None, dev: dict | None = None) -> dict:
    base_on = {"on": True, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}}
    return {"dev": dev or copy.deepcopy(base_on), "staging": staging or copy.deepcopy(base_on), "prod": prod}


SEGMENTS = [
    {
        "key": "internal-staff",
        "name": "Internal staff",
        "included": [],
        "excluded": [],
        "rules": [{"clauses": [{"attribute": "email", "op": "ends_with", "values": ["@larkspur.test"]}]}],
    },
    {
        "key": "beta-testers",
        "name": "Beta testers",
        "included": ["user-00007", "user-00042", "user-00314"],
        "excluded": ["user-00666"],
        "rules": [{"clauses": [{"attribute": "beta_opt_in", "op": "equals", "values": [True]}]}],
    },
    {
        "key": "enterprise-accounts",
        "name": "Enterprise accounts",
        "included": [],
        "excluded": [],
        "rules": [{"clauses": [{"attribute": "plan", "op": "in", "values": ["enterprise"]}]}],
    },
]


def flags() -> list[tuple[dict, int]]:
    """(flag, age in days)."""
    staff_rule = {
        "id": "staff",
        "description": "Staff always see it",
        "clauses": [{"attribute": "key", "op": "segment_match", "values": ["internal-staff"]}],
        "variation": 0,
    }
    return [
        (
            {
                "key": "new-checkout",
                "name": "New checkout flow",
                "description": "One page checkout. A/B test against the current three step flow.",
                "kind": "boolean",
                "variations": [{"value": True, "name": "one-page"}, {"value": False, "name": "three-step"}],
                "tags": ["critical", "checkout", "experiment"],
                "client_side": True,
                "experiment": {
                    "primary_metric": "checkout_completed",
                    "control": 1,
                    "hypothesis": "A one page checkout raises completed checkouts by at least 5% relative.",
                },
                "environments": _envs({"on": True, "off_variation": 1, "rules": [], "fallthrough": _rollout(50)}),
            },
            45,
        ),
        (
            {
                "key": "search-v2",
                "name": "Search v2 ranking",
                "description": "New ranking model. Gradual rollout, old app versions excluded.",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["search"],
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 1,
                        "rules": [
                            {
                                "id": "old-ios",
                                "description": "iOS builds before 4.2 cannot render the new results card",
                                "clauses": [
                                    {"attribute": "platform", "op": "equals", "values": ["ios"]},
                                    {"attribute": "app_version", "op": "semver_lt", "values": ["4.2.0"]},
                                ],
                                "variation": 1,
                            },
                            {**staff_rule, "id": "staff"},
                        ],
                        "fallthrough": _rollout(1),
                    }
                ),
            },
            30,
        ),
        (
            {
                "key": "pricing-page-layout",
                "name": "Pricing page layout",
                "kind": "string",
                "variations": [
                    {"value": "classic", "name": "classic"},
                    {"value": "compact", "name": "compact"},
                    {"value": "annual-first", "name": "annual first"},
                ],
                "tags": ["growth"],
                "client_side": True,
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 0,
                        "rules": [
                            {
                                "id": "na",
                                "description": "North America sees annual pricing first",
                                "clauses": [{"attribute": "country", "op": "in", "values": ["US", "CA"]}],
                                "variation": 2,
                            },
                            {
                                "id": "mobile-web",
                                "clauses": [
                                    {
                                        "attribute": "user_agent",
                                        "op": "matches",
                                        "values": ["(?i)mobile|android|iphone"],
                                    }
                                ],
                                "variation": 1,
                            },
                        ],
                        "fallthrough": {"variation": 0},
                    },
                    staging={"on": True, "off_variation": 0, "rules": [], "fallthrough": {"variation": 1}},
                    dev={"on": True, "off_variation": 0, "rules": [], "fallthrough": {"variation": 1}},
                ),
            },
            60,
        ),
        (
            {
                "key": "recommendations-model",
                "name": "Recommendations model config",
                "kind": "json",
                "variations": [
                    {"value": {"model": "als-v3", "k": 20, "diversity": 0.1}, "name": "als-v3"},
                    {"value": {"model": "two-tower-v1", "k": 40, "diversity": 0.3}, "name": "two-tower-v1"},
                ],
                "tags": ["ml"],
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 0,
                        "rules": [
                            {
                                "id": "heavy-buyers",
                                "description": "Customers with 10 to 1000 orders get the new model at 30%",
                                "clauses": [{"attribute": "orders", "op": "between", "values": [[10, 1000]]}],
                                "rollout": {"weights": [70_000, 30_000], "bucket_by": "key"},
                            }
                        ],
                        "fallthrough": {"variation": 0},
                    }
                ),
            },
            20,
        ),
        (
            {
                "key": "max-cart-items",
                "name": "Max cart items",
                "kind": "number",
                "variations": [
                    {"value": 50, "name": "50"},
                    {"value": 100, "name": "100"},
                    {"value": 250, "name": "250"},
                ],
                "tags": ["checkout"],
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 0,
                        "rules": [
                            {
                                "id": "enterprise",
                                "clauses": [
                                    {"attribute": "key", "op": "segment_match", "values": ["enterprise-accounts"]}
                                ],
                                "variation": 2,
                            },
                            {
                                "id": "new-signups",
                                "description": "Accounts created after the 2026 pricing change",
                                "clauses": [
                                    {"attribute": "signup_date", "op": "after", "values": ["2026-03-01T00:00:00Z"]}
                                ],
                                "variation": 1,
                            },
                        ],
                        "fallthrough": {"variation": 0},
                    }
                ),
            },
            90,
        ),
        (
            {
                "key": "ledger-sync",
                "name": "Ledger sync backend",
                "description": "Dark launch: writes go to the new ledger service in shadow mode.",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["critical", "payments"],
                "environments": _envs(
                    {"on": False, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}},
                    staging={"on": False, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}},
                ),
            },
            14,
        ),
        (
            {
                "key": "ledger-sync-ui",
                "name": "Ledger sync status page",
                "description": "Only makes sense when ledger-sync serves on.",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["payments"],
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 1,
                        "prerequisites": [{"key": "ledger-sync", "variation": 0}],
                        "rules": [],
                        "fallthrough": {"variation": 0},
                    },
                    staging={
                        "on": True,
                        "off_variation": 1,
                        "prerequisites": [{"key": "ledger-sync", "variation": 0}],
                        "rules": [],
                        "fallthrough": {"variation": 0},
                    },
                ),
            },
            10,
        ),
        (
            {
                "key": "payments-retry-policy",
                "name": "Payments retry policy",
                "kind": "string",
                "variations": [
                    {"value": "fixed-3", "name": "fixed 3 tries"},
                    {"value": "exp-backoff-5", "name": "exponential, 5 tries"},
                ],
                "tags": ["critical", "payments", "permanent"],
                "environments": _envs({"on": True, "off_variation": 0, "rules": [], "fallthrough": {"variation": 0}}),
            },
            75,
        ),
        (
            {
                "key": "legacy-banner",
                "name": "Spring sale banner",
                "description": "Rolled out to 100% months ago. Nobody removed it.",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["marketing"],
                "client_side": True,
                "environments": _envs({"on": True, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}}),
            },
            160,
        ),
        (
            {
                "key": "holiday-promo-2025",
                "name": "Holiday promo 2025",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["marketing"],
                "environments": _envs({"on": False, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}}),
            },
            320,
        ),
        (
            {
                "key": "pdf-export-beta",
                "name": "PDF export beta",
                "kind": "boolean",
                "variations": BOOL,
                "tags": ["reports"],
                "environments": _envs(
                    {
                        "on": True,
                        "off_variation": 1,
                        "rules": [
                            {
                                "id": "beta",
                                "clauses": [{"attribute": "key", "op": "segment_match", "values": ["beta-testers"]}],
                                "variation": 0,
                            }
                        ],
                        "fallthrough": {"variation": 1},
                    }
                ),
            },
            90,
        ),
    ]


def synthetic_user(rng: random.Random, i: int) -> dict:
    country = rng.choices(["US", "CA", "GB", "DE", "BR", "IN"], [40, 8, 14, 12, 12, 14])[0]
    platform = rng.choices(["web", "ios", "android"], [55, 25, 20])[0]
    return {
        "key": f"user-{i:05d}",
        "email": f"shopper{i}@example.test" if rng.random() > 0.01 else f"staff{i}@larkspur.test",
        "country": country,
        "platform": platform,
        "app_version": rng.choice(["4.0.3", "4.1.9", "4.2.0", "4.3.1", "5.0.0-beta.2"]),
        "plan": rng.choices(["free", "pro", "enterprise"], [70, 25, 5])[0],
        "orders": int(rng.expovariate(1 / 8)),
        "signup_date": f"202{rng.randint(4, 6)}-{rng.randint(1, 12):02d}-15",
        "beta_opt_in": rng.random() < 0.05,
        "user_agent": "Mozilla/5.0 (iPhone)" if platform == "ios" else "Mozilla/5.0 (X11; Linux)",
    }


def simulate_experiment(
    store: Store,
    flag_key: str = "new-checkout",
    env: str = "prod",
    users: int = 20_000,
    base_rate: float = 0.10,
    relative_lift: float = 0.12,
    seed: int = 7,
    start: float | None = None,
) -> dict:
    """Run synthetic traffic through the real engine and record impressions and conversions.

    The treatment (the flag's non-control variation) converts at base_rate * (1 + relative_lift).
    Uses a fixed seed, so the readout is the same on every run.
    """
    rng = random.Random(seed)
    rs = store.ruleset(env)
    flag = store.get_flag(flag_key)
    control = flag["experiment"].get("control", 0)
    start = start if start is not None else store.clock() - 7 * DAY
    other_flags = [k for k in rs["flags"] if k not in (flag_key, "pdf-export-beta", "holiday-promo-2025")]
    events, counts = [], [0] * len(flag["variations"])
    for i in range(users):
        user = synthetic_user(rng, i)
        ts = start + rng.random() * 7 * DAY
        d = engine.evaluate(rs, flag_key, user)
        if d.variation is None:
            continue
        counts[d.variation] += 1
        events.append(
            {"type": "impression", "flag": flag_key, "variation": d.variation, "user_key": user["key"], "ts": ts}
        )
        # Other flags get evaluated too, so stale detection sees them as live.
        if i % 50 == 0:
            for k in other_flags:
                od = engine.evaluate(rs, k, user)
                events.append(
                    {"type": "impression", "flag": k, "variation": od.variation, "user_key": user["key"], "ts": ts}
                )
        rate = base_rate if d.variation == control else base_rate * (1 + relative_lift)
        if rng.random() < rate:
            events.append(
                {"type": "conversion", "metric": "checkout_completed", "user_key": user["key"], "ts": ts + 60}
            )
        if len(events) >= 5000:
            store.record_events(env, events)
            events = []
    store.record_events(env, events)
    return {"users": users, "per_variation": counts}


def seed(store: Store, simulate: bool = True) -> None:
    real_clock = store.clock
    now = real_clock()
    try:
        for seg in SEGMENTS:
            store.clock = lambda: now - 200 * DAY
            store.put_segment(seg, "seed")
        for flag, age in flags():
            store.clock = lambda age=age: now - age * DAY
            # A fixed salt keeps bucketing, and so the simulated readout, identical on every run.
            store.create_flag({**flag, "salt": f"{flag['key']}-2026"}, "alice")
        # A rollout history for search-v2: 5% -> 10% -> 25% over two weeks, with diffs in the audit log.
        for days_ago, pct, who in [(28, 5, "alice"), (21, 10, "bob"), (14, 25, "alice")]:
            store.clock = lambda d=days_ago: now - d * DAY
            f = store.get_flag("search-v2")
            f["environments"]["prod"]["fallthrough"] = _rollout(pct)
            store.update_flag("search-v2", f, f["version"], who)
        store.clock = lambda: now - 3 * DAY
        store.set_env_state("ledger-sync", "staging", "carol", on=True)
    finally:
        store.clock = real_clock
    # A pending four-eyes request: bob wants exponential retries in prod.
    f = store.get_flag("payments-retry-policy")
    f["environments"]["prod"]["fallthrough"] = {"variation": 1}
    store.update_flag("payments-retry-policy", f, f["version"], "bob")
    # A scheduled change: search-v2 goes to 50% tomorrow morning.
    store.create_schedule("search-v2", "prod", now + DAY, {"rollout_percent": 50}, "alice")
    if simulate:
        simulate_experiment(store)


if __name__ == "__main__":
    s = Store(":memory:")
    t = time.perf_counter()
    seed(s)
    print(f"seeded in {time.perf_counter() - t:.2f}s")
