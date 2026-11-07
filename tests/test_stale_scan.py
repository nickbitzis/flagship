import json
from pathlib import Path

from conftest import bool_flag

from flagship.cli import main
from flagship.scan import scan, to_markdown
from flagship.stale import DAY, find_stale

NOW = 1_800_000_000.0


def aged(flag, days, updated_days=None):
    flag["created_at"] = NOW - days * DAY
    flag["updated_at"] = NOW - (updated_days if updated_days is not None else days) * DAY
    return flag


def rollout(pct):
    return {"rollout": {"weights": [pct * 1000, 100_000 - pct * 1000]}}


def test_constant_flags_are_stale_after_n_days():
    full = aged(bool_flag("full"), 60)  # on, fallthrough serves true: a finished 100% rollout
    off = aged(bool_flag("gone"), 90)
    off["environments"]["prod"]["on"] = False
    partial = aged(bool_flag("partial"), 90)
    partial["environments"]["prod"]["fallthrough"] = rollout(40)
    hundred = aged(bool_flag("hundred"), 90)
    hundred["environments"]["prod"]["fallthrough"] = rollout(100)
    recent = aged(bool_flag("recent"), 90, updated_days=5)
    stats = {k: {"count": 1, "last_ts": NOW} for k in ("full", "gone", "partial", "hundred", "recent")}
    out = {
        s["key"]: s for s in find_stale({"flags": [full, off, partial, hundred, recent], "eval_stats": stats}, NOW, 30)
    }
    assert set(out) == {"full", "gone", "hundred"}
    assert out["gone"]["value"] is False and out["full"]["value"] is True and out["full"]["days"] == 60


def test_rules_make_a_flag_not_constant():
    f = aged(bool_flag("ruled"), 90)
    f["environments"]["prod"]["rules"] = [{"clauses": [], "variation": 1}]
    assert find_stale({"flags": [f], "eval_stats": {"ruled": {"last_ts": NOW}}}, NOW, 30) == []


def test_never_and_not_recently_evaluated():
    a = aged(bool_flag("never"), 45)
    a["environments"]["prod"]["fallthrough"] = rollout(50)
    b = aged(bool_flag("old-use"), 45)
    b["environments"]["prod"]["fallthrough"] = rollout(50)
    c = aged(bool_flag("young"), 10)
    c["environments"]["prod"]["fallthrough"] = rollout(50)
    perm = aged(bool_flag("kill-switch", tags=["permanent"]), 400)
    export = {"flags": [a, b, c, perm], "eval_stats": {"old-use": {"count": 9, "last_ts": NOW - 40 * DAY}}}
    out = {s["key"]: s["kind"] for s in find_stale(export, NOW, 30)}
    assert out == {"never": "never_evaluated", "old-use": "not_evaluated_recently"}


def test_scanner_buckets(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "checkout.py").write_text(
        'if client.variation("still-used", user, False):\n    pass\n'
        'banner = client.is_enabled("stale-but-used", user)\n'
        'x = client.variation("not-on-server", user, 1)\n'
    )
    (tmp_path / "web.ts").write_text("const on = flags.variation('still-used', ctx, false);\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("variation('stale-unused')\n")  # skipped dir
    used = aged(bool_flag("still-used"), 90)
    used["environments"]["prod"]["fallthrough"] = rollout(30)
    export = {
        "flags": [used, aged(bool_flag("stale-but-used"), 90), aged(bool_flag("stale-unused"), 90)],
        "eval_stats": {k: {"count": 1, "last_ts": NOW} for k in ("still-used", "stale-but-used", "stale-unused")},
    }
    r = scan(tmp_path, export, NOW, 30)
    assert [x["key"] for x in r["remove_flag"]] == ["stale-unused"]
    assert [x["key"] for x in r["clean_up"]] == ["stale-but-used"]
    assert r["clean_up"][0]["references"] == ["svc/checkout.py:3"]
    assert r["missing"] == ["not-on-server"]
    assert r["unreferenced"] == []
    assert r["summary"]["files_scanned"] == 2
    md = to_markdown(r, 30)
    assert "| `stale-unused` |" in md and "`svc/checkout.py:3`" in md


def test_scan_cli_on_example_app(capsys):
    root = Path(__file__).resolve().parents[1] / "examples"
    main(["scan", str(root / "shop"), "--flags", str(root / "flags-export.json"), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert [x["key"] for x in report["remove_flag"]] == ["pdf-export-beta"]
    assert {x["key"] for x in report["clean_up"]} == {"legacy-banner", "holiday-promo-2025"}
    assert report["missing"] == ["gift-wrap"]
