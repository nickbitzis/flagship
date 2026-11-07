import collections

import pytest
from conftest import flat, ruleset

from flagship import engine, operators


def rule(clauses, variation=0, rid="r"):
    return {"id": rid, "clauses": clauses, "variation": variation}


def clause(attr, op, values, negate=False):
    return {"attribute": attr, "op": op, "values": values, "negate": negate}


def eval_one(flag, ctx, segments=()):
    return engine.evaluate(ruleset(flag, segments=segments), flag["key"], ctx)


@pytest.mark.parametrize(
    "op,user,values,expected",
    [
        ("equals", "US", ["US"], True),
        ("equals", "us", ["US"], False),
        ("equals", 3, [3.0], True),
        ("equals", "3", [3], False),
        ("in", "CA", ["US", "CA"], True),
        ("in", "MX", ["US", "CA"], False),
        ("contains", "pro-annual", ["annual"], True),
        ("contains", 42, ["4"], False),
        ("starts_with", "user-123", ["user-"], True),
        ("ends_with", "ana@larkspur.test", ["@larkspur.test"], True),
        ("matches", "Mozilla/5.0 (iPhone)", ["(?i)iphone|android"], True),
        ("matches", "Mozilla/5.0 (X11)", ["(?i)iphone|android"], False),
        ("matches", "abc", ["("], False),  # a bad regex never raises
        ("lt", 5, [10], True),
        ("lte", 10, [10], True),
        ("gt", "11", [10], True),
        ("gte", 9, [10], False),
        ("between", 10, [[10, 20]], True),
        ("between", 20, [[10, 20]], False),
        ("between", 15, [[0, 5], [10, 20]], True),
        ("semver_eq", "4.2", ["4.2.0"], True),
        ("semver_lt", "4.1.9", ["4.2.0"], True),
        ("semver_lt", "4.10.0", ["4.2.0"], False),  # numeric, not string compare
        ("semver_gt", "5.0.0", ["5.0.0-beta.2"], True),  # release sorts after pre-release
        ("semver_lt", "5.0.0-alpha", ["5.0.0-beta"], True),
        ("semver_gt", "not-a-version", ["1.0.0"], False),
        ("before", "2026-01-01", ["2026-03-01T00:00:00Z"], True),
        ("after", "2026-06-01T12:00:00+02:00", ["2026-03-01T00:00:00Z"], True),
        ("after", 1_780_000_000_000, ["2026-01-01"], True),  # epoch milliseconds
        ("before", "yesterday", ["2026-01-01"], False),
    ],
)
def test_operators(op, user, values, expected):
    assert operators.apply(op, user, values) is expected


def test_list_attribute_matches_any_element():
    assert operators.apply("in", ["admin", "beta"], ["beta"]) is True


def test_negate_and_missing_attribute():
    f = flat(rules=[rule([clause("country", "in", ["US"], negate=True)])], fallthrough={"variation": 1})
    assert eval_one(f, {"key": "u", "country": "DE"}).value is True
    assert eval_one(f, {"key": "u", "country": "US"}).value is False
    # A missing attribute does not match, and negating a non-match makes it match.
    assert eval_one(f, {"key": "u"}).reason["kind"] == "RULE_MATCH"


def test_nested_attribute_lookup():
    f = flat(rules=[rule([clause("device.os", "equals", ["ios"])])], fallthrough={"variation": 1})
    assert eval_one(f, {"key": "u", "device": {"os": "ios"}}).value is True


def test_rules_evaluated_in_order_first_match_wins():
    f = flat(
        variations=[{"value": "a"}, {"value": "b"}, {"value": "c"}],
        off_variation=0,
        rules=[
            rule([clause("country", "in", ["US"])], 1, "first"),
            rule([clause("plan", "in", ["pro"])], 2, "second"),
        ],
        fallthrough={"variation": 0},
    )
    d = eval_one(f, {"key": "u", "country": "US", "plan": "pro"})
    assert (d.value, d.reason) == ("b", {"kind": "RULE_MATCH", "rule_index": 0, "rule_id": "first"})
    d = eval_one(f, {"key": "u", "country": "DE", "plan": "pro"})
    assert d.value == "c" and d.reason["rule_index"] == 1
    assert eval_one(f, {"key": "u"}).reason == {"kind": "FALLTHROUGH"}


def test_all_clauses_in_a_rule_must_match():
    f = flat(rules=[rule([clause("platform", "equals", ["ios"]), clause("app_version", "semver_lt", ["4.2.0"])], 1)])
    assert eval_one(f, {"key": "u", "platform": "ios", "app_version": "4.1.0"}).value is False
    assert eval_one(f, {"key": "u", "platform": "ios", "app_version": "4.3.0"}).value is True


def test_off_and_kill_switch_serve_off_variation():
    assert eval_one(flat(on=False), {"key": "u"}).reason == {"kind": "OFF"}
    d = eval_one(flat(killed=True), {"key": "u"})
    assert d.value is False and d.reason == {"kind": "OFF", "killed": True}


def test_individual_targets_beat_rules():
    f = flat(targets=[{"variation": 1, "values": ["vip"]}], rules=[rule([], 0)])
    assert eval_one(f, {"key": "vip"}).reason == {"kind": "TARGET_MATCH"}


def test_segments_include_exclude_and_rules():
    seg = {
        "key": "beta",
        "included": ["u1"],
        "excluded": ["u2"],
        "rules": [{"clauses": [clause("email", "ends_with", ["@larkspur.test"])]}],
    }
    f = flat(rules=[rule([clause("key", "segment_match", ["beta"])], 0)], fallthrough={"variation": 1})
    assert eval_one(f, {"key": "u1"}, [seg]).value is True
    assert eval_one(f, {"key": "u2", "email": "x@larkspur.test"}, [seg]).value is False  # exclusion wins
    assert eval_one(f, {"key": "u3", "email": "x@larkspur.test"}, [seg]).value is True
    assert eval_one(f, {"key": "u4"}, [seg]).value is False


def test_errors_return_default_with_reason():
    rs = ruleset(flat(rules=[{"clauses": [{"attribute": "a", "op": "bogus", "values": [1]}], "variation": 0}]))
    d = engine.evaluate(rs, "f", {"key": "u", "a": 1}, default="dflt")
    assert d.value == "dflt" and d.reason["kind"] == "ERROR" and d.reason["error_kind"] == "MALFORMED_FLAG"
    assert engine.evaluate(rs, "nope", {"key": "u"}, 7).reason["error_kind"] == "FLAG_NOT_FOUND"
    assert engine.evaluate(rs, "f", {}, 7).reason["error_kind"] == "USER_KEY_MISSING"


def test_bucketing_is_deterministic():
    assert engine.bucket("search-v2", "abc", "user-1") == engine.bucket("search-v2", "abc", "user-1")
    assert engine.bucket("search-v2", "abc", "user-1") != engine.bucket("search-v2", "other-salt", "user-1")
    f = flat(fallthrough={"rollout": {"weights": [30_000, 70_000]}})
    first = [eval_one(f, {"key": f"u{i}"}).value for i in range(500)]
    assert first == [eval_one(f, {"key": f"u{i}"}).value for i in range(500)]


def test_users_in_at_10_percent_stay_in_as_rollout_grows():
    users = [{"key": f"user-{i}"} for i in range(5000)]

    def enabled(pct):
        f = flat(fallthrough={"rollout": {"weights": [pct * 1000, 100_000 - pct * 1000]}})
        return {u["key"] for u in users if eval_one(f, u).value is True}

    at = {p: enabled(p) for p in (10, 20, 50, 100)}
    assert at[10] <= at[20] <= at[50] <= at[100]
    assert len(at[100]) == 5000
    assert 400 < len(at[10]) < 600


def test_bucket_distribution_is_uniform():
    # Chi-square over 10 equal bins, 20,000 users. Critical value for df=9 at p=0.001 is 27.9.
    counts = collections.Counter(engine.bucket("flag", "salt", f"user-{i}") // 10_000 for i in range(20_000))
    expected = 2000
    chi2 = sum((counts[b] - expected) ** 2 / expected for b in range(10))
    assert chi2 < 27.9


def test_multivariate_weights_split_traffic():
    f = flat(
        variations=[{"value": "a"}, {"value": "b"}, {"value": "c"}],
        off_variation=0,
        fallthrough={"rollout": {"weights": [50_000, 30_000, 20_000]}},
    )
    c = collections.Counter(eval_one(f, {"key": f"u{i}"}).value for i in range(10_000))
    assert abs(c["a"] / 10_000 - 0.5) < 0.02 and abs(c["b"] / 10_000 - 0.3) < 0.02 and abs(c["c"] / 10_000 - 0.2) < 0.02


def test_rollout_by_other_attribute_and_missing_attribute():
    f = flat(fallthrough={"rollout": {"weights": [50_000, 50_000], "bucket_by": "org"}})
    a = eval_one(f, {"key": "u1", "org": "acme"}).value
    assert all(eval_one(f, {"key": f"u{i}", "org": "acme"}).value == a for i in range(20))  # whole org together
    assert eval_one(f, {"key": "u1"}).reason["error_kind"] == "USER_ATTRIBUTE_MISSING"


def test_prerequisites():
    parent = flat("parent", fallthrough={"variation": 0})
    child = flat("child", prerequisites=[{"key": "parent", "variation": 0}])
    rs = ruleset(parent, child)
    d = engine.evaluate(rs, "child", {"key": "u"})
    assert d.value is True and d.reason == {"kind": "FALLTHROUGH"}
    assert d.prereq_events[0][0] == "parent"
    parent["on"] = False
    d = engine.evaluate(rs, "child", {"key": "u"})
    assert d.value is False and d.reason == {"kind": "PREREQUISITE_FAILED", "prerequisite_key": "parent"}
    parent["on"] = True
    parent["fallthrough"] = {"variation": 1}  # parent on, but serves the wrong variation
    assert engine.evaluate(rs, "child", {"key": "u"}).reason["kind"] == "PREREQUISITE_FAILED"


def test_prerequisite_cycle_at_eval_time_is_an_error_not_a_crash():
    a = flat("a", prerequisites=[{"key": "b", "variation": 0}])
    b = flat("b", prerequisites=[{"key": "a", "variation": 0}])
    d = engine.evaluate(ruleset(a, b), "a", {"key": "u"}, default=False)
    assert d.reason["error_kind"] == "PREREQUISITE_CYCLE" and d.value is False


def test_find_cycle():
    assert engine.find_cycle({"a": ["b"], "b": ["c"], "c": []}) is None
    assert engine.find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert engine.find_cycle({"x": ["x"]}) == ["x", "x"]


def test_json_and_number_variations():
    f = flat(
        variations=[{"value": {"model": "als-v3", "k": 20}}, {"value": 250}],
        rules=[rule([clause("orders", "between", [[10, 1000]])], 1)],
        off_variation=0,
    )
    assert eval_one(f, {"key": "u", "orders": 12}).value == 250
    assert eval_one(f, {"key": "u", "orders": 2}).value == {"model": "als-v3", "k": 20}
