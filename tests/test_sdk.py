import random

from conftest import LiveServer, bool_flag, wait_for

from flagship.sdk import FlagshipClient
from flagship.store import Store

KEY = "sdk-prod-demo"
USER = {"key": "user-1", "country": "US"}


def make_client(url, **kw):
    kw.setdefault("flush_interval", 0.1)
    kw.setdefault("backoff_initial", 0.05)
    kw.setdefault("backoff_max", 0.2)
    kw.setdefault("rng", random.Random(1))
    return FlagshipClient(url, KEY, **kw)


def test_sdk_evaluates_locally_and_streams_updates(live):
    live.store.create_flag(bool_flag("checkout"), "alice")
    c = make_client(live.url)
    try:
        assert c.source == "server" and c.variation("checkout", USER, False) is True
        assert wait_for(lambda: c.connected)
        rev = c.revision
        live.store.set_env_state("checkout", "prod", "alice", on=False)  # kill switch
        assert wait_for(lambda: c.revision and c.revision > rev), "SSE update never arrived"
        d = c.variation_detail("checkout", USER, True)
        assert d.value is False and d.reason == {"kind": "OFF"}
        assert c.source == "stream"
    finally:
        c.close()


def test_app_keeps_working_when_server_dies(live, tmp_path):
    live.store.create_flag(bool_flag("checkout"), "alice")
    f = live.store.get_flag("checkout")
    f["environments"]["prod"]["rules"] = [
        {"id": "ca", "clauses": [{"attribute": "country", "op": "in", "values": ["CA"]}], "variation": 1}
    ]
    live.store.update_flag("checkout", f, f["version"], "alice")
    cache = tmp_path / "cache.json"
    c = make_client(live.url, cache_path=cache)
    try:
        assert c.variation("checkout", {"key": "u", "country": "CA"}, True) is False
        live.stop()  # the flag server goes away
        assert wait_for(lambda: not c.connected)
        # 1. In memory: same answers, same reasons, no network needed.
        for _ in range(100):
            assert c.variation("checkout", {"key": "u", "country": "CA"}, True) is False
            assert c.variation("checkout", USER, False) is True
        # Impressions queue up instead of being lost.
        assert c.flush() is False and c.pending_events() > 0
    finally:
        c.close(flush=False)
    # 2. A process that starts while the server is down uses the last known good ruleset on disk.
    c2 = make_client(live.url, cache_path=cache)
    try:
        assert c2.source == "cache"
        assert c2.variation("checkout", {"key": "u", "country": "CA"}, True) is False
    finally:
        c2.close(flush=False)
    # 3. No server and no cache: the defaults in code.
    c3 = make_client(live.url, cache_path=tmp_path / "none.json")
    try:
        d = c3.variation_detail("checkout", USER, "code-default")
        assert c3.source == "defaults" and d.value == "code-default"
        assert d.reason == {"kind": "ERROR", "error_kind": "CLIENT_NOT_READY"}
    finally:
        c3.close(flush=False)


def test_sdk_reconnects_and_resyncs_after_restart(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    store.create_flag(bool_flag("flag-a"), "alice")
    server = LiveServer(store).start()
    c = make_client(server.url)
    try:
        assert wait_for(lambda: c.connected)
        server.stop()
        assert wait_for(lambda: not c.connected)
        store.set_env_state("flag-a", "prod", "alice", on=False)  # changed while the SDK was cut off
        server2 = LiveServer(store, port=server.port).start()
        try:
            assert wait_for(lambda: c.connected, timeout=8)
            assert wait_for(lambda: c.variation("flag-a", USER, True) is False)
            assert c.reconnects >= 1
        finally:
            server2.stop()
    finally:
        c.close(flush=False)


def test_impressions_are_batched_and_summarized(live):
    live.store.create_flag(bool_flag("flag-b"), "alice")
    c = make_client(live.url, flush_interval=60, stream=False)
    try:
        for _ in range(500):
            c.variation("flag-b", USER)
        c.track("checkout_completed", USER)
        assert c.pending_events() == 2  # 500 evaluations of one user summarize to one impression
        assert c.flush() is True
        stats = live.store.eval_stats()
        assert stats["flag-b"]["count"] == 500
        n = live.store.db.execute("SELECT COUNT(*) FROM conversions").fetchone()[0]
        assert n == 1
    finally:
        c.close()


def test_event_queue_is_bounded(tmp_path):
    c = make_client("http://127.0.0.1:9", stream=False, send_events=False, max_queue=10, timeout=0.2)
    try:
        c._set_ruleset(
            {"env": "prod", "version": 1, "flags": {"flag-c": _flat()}, "segments": {}}, "server", persist=False
        )
        for i in range(25):
            c.variation("flag-c", {"key": f"u{i}"})
        assert c.pending_events() == 10 and c.dropped_events == 15
    finally:
        c.close(flush=False)


def test_prerequisite_evaluations_record_impressions(live):
    live.store.create_flag(bool_flag("parent"), "alice")
    child = bool_flag("child")
    child["environments"]["prod"]["prerequisites"] = [{"key": "parent", "variation": 0}]
    live.store.create_flag(child, "alice")
    c = make_client(live.url, stream=False, flush_interval=60)
    try:
        assert c.variation("child", USER) is True
        c.flush()
        assert set(live.store.eval_stats()) == {"parent", "child"}
    finally:
        c.close()


def _flat():
    return {
        "key": "flag-c",
        "variations": [{"value": True}, {"value": False}],
        "salt": "s",
        "on": True,
        "off_variation": 1,
        "rules": [],
        "fallthrough": {"variation": 0},
    }
