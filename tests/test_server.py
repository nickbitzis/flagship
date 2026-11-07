import copy

import pytest
from conftest import bool_flag

from flagship.store import Conflict, Forbidden, StoreError

H = {"x-actor": "alice"}
BOB = {"x-actor": "bob"}


def test_create_and_get_flag(client):
    r = client.post("/api/flags", json=bool_flag("checkout"), headers=H)
    assert r.status_code == 201
    f = r.json()
    assert f["version"] == 1 and f["salt"] and set(f["environments"]) == {"dev", "staging", "prod"}
    assert client.get("/api/flags/checkout").json()["key"] == "checkout"
    assert client.post("/api/flags", json=bool_flag("checkout"), headers=H).status_code == 409


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda f: f.update(key="Bad Key!"), "key must be"),
        (lambda f: f.update(kind="string"), "does not match kind"),
        (lambda f: f["environments"]["prod"].update(fallthrough={"rollout": {"weights": [10, 20]}}), "weights sum"),
        (
            lambda f: f["environments"]["prod"].update(
                rules=[{"clauses": [{"attribute": "a", "op": "nope", "values": [1]}], "variation": 0}]
            ),
            "unknown operator",
        ),
        (
            lambda f: f["environments"]["prod"].update(
                rules=[{"clauses": [{"attribute": "a", "op": "matches", "values": ["("]}], "variation": 0}]
            ),
            "bad regex",
        ),
        (
            lambda f: f["environments"]["prod"].update(
                rules=[{"clauses": [{"attribute": "v", "op": "semver_lt", "values": ["x.y"]}], "variation": 0}]
            ),
            "semantic version",
        ),
        (
            lambda f: f["environments"]["prod"].update(
                rules=[{"clauses": [{"attribute": "key", "op": "segment_match", "values": ["ghost"]}], "variation": 0}]
            ),
            "unknown segment",
        ),
        (lambda f: f["environments"]["prod"].update(off_variation=5), "off_variation"),
        (
            lambda f: f["environments"]["prod"].update(prerequisites=[{"key": "missing", "variation": 0}]),
            "unknown prerequisite",
        ),
    ],
)
def test_validation_rejects_bad_flags(client, mutate, message):
    f = bool_flag("vflag")
    mutate(f)
    r = client.post("/api/flags", json=f, headers=H)
    assert r.status_code == 400
    assert any(message in e for e in r.json()["errors"]), r.json()


def test_prerequisite_cycle_rejected_on_write(store):
    store.create_flag(bool_flag("aa"), "alice")
    b = bool_flag("bb")
    b["environments"]["prod"]["prerequisites"] = [{"key": "aa", "variation": 0}]
    store.create_flag(b, "alice")
    a = store.get_flag("aa")
    a["environments"]["prod"]["prerequisites"] = [{"key": "bb", "variation": 0}]
    with pytest.raises(StoreError) as e:
        store.update_flag("aa", a, a["version"], "alice")
    assert any("prerequisite cycle" in x and "aa -> bb" in x for x in e.value.extra["errors"])


def test_optimistic_concurrency(client):
    client.post("/api/flags", json=bool_flag("oc"), headers=H)
    f1 = client.get("/api/flags/oc").json()
    f2 = copy.deepcopy(f1)
    f1["description"] = "alice's edit"
    assert client.put("/api/flags/oc", json=f1, headers=H).status_code == 200
    f2["description"] = "bob's stale edit"
    r = client.put("/api/flags/oc", json=f2, headers=BOB)
    assert r.status_code == 409 and r.json()["current_version"] == 2
    del f2["version"]
    assert client.put("/api/flags/oc", json=f2, headers=BOB).status_code == 428
    assert client.get("/api/flags/oc").json()["description"] == "alice's edit"


def test_writes_require_actor(client):
    assert client.post("/api/flags", json=bool_flag("x1")).status_code == 400


def test_audit_log_has_diffs_and_is_append_only(client, store):
    client.post("/api/flags", json=bool_flag("aud"), headers=H)
    f = client.get("/api/flags/aud").json()
    f["environments"]["prod"]["fallthrough"] = {"rollout": {"weights": [10_000, 90_000], "bucket_by": "key"}}
    client.put("/api/flags/aud", json=f, headers=BOB)
    entries = client.get("/api/audit", params={"key": "aud"}).json()["entries"]
    assert [e["action"] for e in entries] == ["flag.update", "flag.create"]
    upd = entries[0]
    assert upd["actor"] == "bob"
    paths = {d["path"]: d for d in upd["diff"]}
    assert paths["environments.prod.fallthrough.variation"] == {
        "path": "environments.prod.fallthrough.variation",
        "before": 0,
        "after": None,
    }
    assert paths["environments.prod.fallthrough.rollout"]["after"]["weights"] == [10_000, 90_000]
    assert paths["version"] == {"path": "version", "before": 1, "after": 2}
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("UPDATE audit SET actor='mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("DELETE FROM audit")


def test_critical_prod_change_needs_a_second_person(client):
    client.post("/api/flags", json=bool_flag("pay", tags=["critical"]), headers=H)
    f = client.get("/api/flags/pay").json()
    f["environments"]["prod"]["fallthrough"] = {"variation": 1}
    r = client.put("/api/flags/pay", json=f, headers=H)
    assert r.status_code == 202
    cr = r.json()["change_request"]
    assert client.get("/api/flags/pay").json()["version"] == 1  # nothing applied yet
    assert client.post(f"/api/approvals/{cr['id']}/approve", headers=H).status_code == 403  # own change
    r = client.post(f"/api/approvals/{cr['id']}/approve", headers=BOB)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    g = client.get("/api/flags/pay").json()
    assert g["version"] == 2 and g["environments"]["prod"]["fallthrough"] == {"variation": 1}
    actions = [e["action"] for e in client.get("/api/audit", params={"key": "pay"}).json()["entries"]]
    assert actions[:3] == ["flag.update", "approval.approve", "approval.request"]


def test_critical_flag_in_staging_and_turning_off_in_prod_skip_approval(client):
    client.post("/api/flags", json=bool_flag("crit", tags=["critical"]), headers=H)
    f = client.get("/api/flags/crit").json()
    f["environments"]["staging"]["fallthrough"] = {"variation": 1}
    assert client.put("/api/flags/crit", json=f, headers=H).status_code == 200
    # Kill switch in prod: the safe direction never waits.
    r = client.post("/api/flags/crit/environments/prod", json={"on": False}, headers=H)
    assert r.status_code == 200 and r.json()["flag"]["environments"]["prod"]["on"] is False
    # Turning it back on does wait.
    assert client.post("/api/flags/crit/environments/prod", json={"on": True}, headers=H).status_code == 202


def test_approval_goes_stale_if_flag_changed(store):
    store.create_flag(bool_flag("st", tags=["critical"]), "alice")
    f = store.get_flag("st")
    f["environments"]["prod"]["fallthrough"] = {"variation": 1}
    cr = store.update_flag("st", f, 1, "alice")["change_request"]
    store.set_env_state("st", "dev", "carol", on=False)  # someone else changes the flag
    with pytest.raises(Conflict):
        store.decide(cr["id"], "bob", True)
    assert store.get_change_request(cr["id"])["status"] == "stale"


def test_reject_and_double_decide(store):
    store.create_flag(bool_flag("rj", tags=["critical"]), "alice")
    f = store.get_flag("rj")
    f["environments"]["prod"]["fallthrough"] = {"variation": 1}
    cr = store.update_flag("rj", f, 1, "alice")["change_request"]
    assert store.decide(cr["id"], "alice", False)["status"] == "rejected"  # requester may withdraw
    with pytest.raises(Conflict):
        store.decide(cr["id"], "bob", True)
    with pytest.raises(Forbidden):
        f2 = store.get_flag("rj")
        f2["environments"]["prod"]["fallthrough"] = {"variation": 1}
        cr2 = store.update_flag("rj", f2, 1, "alice")["change_request"]
        store.decide(cr2["id"], "alice", True)


def test_scheduled_changes_apply_when_due(store, clock):
    store.create_flag(bool_flag("sch"), "alice")
    s1 = store.create_schedule("sch", "prod", clock() + 3600, {"rollout_percent": 25}, "alice")
    s2 = store.create_schedule("sch", "prod", clock() + 7200, {"rollout_percent": 50}, "alice")
    assert store.apply_due_schedules() == []
    clock.advance(3601)
    done = store.apply_due_schedules()
    assert [s["id"] for s in done] == [s1["id"]] and done[0]["status"] == "applied"
    w = store.get_flag("sch")["environments"]["prod"]["fallthrough"]["rollout"]["weights"]
    assert w == [25_000, 75_000]
    clock.advance(3600)
    assert [s["id"] for s in store.apply_due_schedules()] == [s2["id"]]
    assert store.get_flag("sch")["environments"]["prod"]["fallthrough"]["rollout"]["weights"] == [50_000, 50_000]
    assert store.audit("sch")[0]["actor"] == "scheduler (alice)"


def test_schedule_on_critical_prod_flag_waits_for_approval(store, clock):
    store.create_flag(bool_flag("cs", tags=["critical"]), "alice")
    s = store.create_schedule("cs", "prod", clock() + 10, {"rollout_percent": 10}, "alice")
    assert s["status"] == "awaiting_approval"
    clock.advance(20)
    assert store.apply_due_schedules() == []
    store.decide(s["change_request"]["id"], "bob", True)
    assert store.apply_due_schedules()[0]["status"] == "applied"


def test_schedule_validation(store, clock):
    store.create_flag(bool_flag("sv"), "alice")
    with pytest.raises(StoreError):
        store.create_schedule("sv", "prod", clock(), {"rollout_percent": 140}, "alice")
    with pytest.raises(StoreError):
        store.create_schedule("sv", "prod", clock(), {"nothing": 1}, "alice")


def test_segments_crud_and_concurrency(client):
    seg = {"name": "Beta", "included": ["u1"], "excluded": [], "rules": []}
    r = client.put("/api/segments/beta", json=seg, headers=H)
    assert r.status_code == 200 and r.json()["version"] == 1
    assert client.put("/api/segments/beta", json={**seg, "version": 7}, headers=H).status_code == 409
    assert client.put("/api/segments/beta", json={**seg, "version": 1}, headers=H).json()["version"] == 2


def test_sdk_keys_server_vs_client(client):
    client.post("/api/flags", json=bool_flag("pub", client_side=True), headers=H)
    client.post("/api/flags", json=bool_flag("secret"), headers=H)
    assert client.get("/sdk/ruleset").status_code == 401
    assert client.get("/sdk/ruleset", headers={"authorization": "client-prod-demo"}).status_code == 403
    rs = client.get("/sdk/ruleset", headers={"authorization": "sdk-prod-demo"}).json()
    assert rs["env"] == "prod" and set(rs["flags"]) == {"pub", "secret"}
    r = client.post("/sdk/evaluate", json={"context": {"key": "u1"}}, headers={"authorization": "client-prod-demo"})
    assert set(r.json()["flags"]) == {"pub"}  # client keys never see server-only flags or any rules


def test_evaluate_endpoint_with_unsaved_draft(client):
    client.post("/api/flags", json=bool_flag("draft"), headers=H)
    f = client.get("/api/flags/draft").json()
    f["environments"]["prod"]["rules"] = [
        {"id": "de", "clauses": [{"attribute": "country", "op": "in", "values": ["DE"]}], "variation": 1}
    ]
    r = client.post("/api/evaluate", json={"env": "prod", "draft": f, "context": {"key": "u", "country": "DE"}})
    body = r.json()
    assert body["value"] is False and body["reason"]["rule_id"] == "de" and body["variation_name"] == "off"
    assert 0 <= body["bucket"] < 100


def test_healthz_metrics_and_ui(client):
    client.post("/api/flags", json=bool_flag("mm"), headers=H)
    assert client.get("/healthz").json()["status"] == "ok"
    text = client.get("/metrics").text
    assert "flagship_flag_changes_total" in text and 'flagship_http_requests_total{method="POST"' in text
    assert "flagship_flags 1" in text
    r = client.get("/")
    assert r.status_code == 200 and "Flagship" in r.text


def test_events_feed_eval_stats(client):
    client.post("/api/flags", json=bool_flag("ev"), headers=H)
    ev = [
        {"type": "impression", "flag": "ev", "variation": 0, "user_key": "u1", "count": 3},
        {"type": "conversion", "metric": "m", "user_key": "u1"},
    ]
    assert client.post("/sdk/events", json={"events": ev}, headers={"authorization": "sdk-prod-demo"}).json() == {
        "accepted": 2
    }
    flags = client.get("/api/flags").json()["flags"]
    assert flags[0]["evaluations"] == 3


def test_noop_update_does_not_bump_version(store):
    store.create_flag(bool_flag("noop"), "alice")
    f = store.get_flag("noop")
    assert store.update_flag("noop", f, 1, "bob")["status"] == "unchanged"
    assert store.get_flag("noop")["version"] == 1
    assert len(store.audit("noop")) == 1


def test_seeded_demo_is_consistent():
    from flagship.seed import seed
    from flagship.store import Store

    s = Store(":memory:")
    seed(s, simulate=False)
    assert len(s.list_flags()) == 11 and len(s.list_segments()) == 3
    assert len(s.list_change_requests()) == 1
    rs = s.ruleset("prod")
    from flagship import engine

    d = engine.evaluate(rs, "search-v2", {"key": "u1", "platform": "ios", "app_version": "4.1.0"})
    assert d.reason["rule_id"] == "old-ios" and d.value is False
    d = engine.evaluate(rs, "ledger-sync-ui", {"key": "u1"})
    assert d.reason == {"kind": "PREREQUISITE_FAILED", "prerequisite_key": "ledger-sync"}
