"""Persistence and the write path: SQLite storage, optimistic concurrency, approvals, schedules, audit.

Every change goes through `_commit`, which bumps the flag version, writes an audit entry with a
diff, bumps the ruleset revision, and calls the change listener (the SSE hub). The audit table has
triggers that reject UPDATE and DELETE, so the log is append-only at the database level.
"""

from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable

from . import engine, validation
from .diff import diff

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, doc TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS segments (key TEXT PRIMARY KEY, doc TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, before TEXT, after TEXT, diff TEXT NOT NULL, note TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS change_requests (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, flag_key TEXT NOT NULL, base_version INTEGER,
  payload TEXT NOT NULL, requested_by TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT, decided_ts REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, flag_key TEXT NOT NULL, env TEXT NOT NULL, run_at REAL NOT NULL, action TEXT NOT NULL,
  created_by TEXT NOT NULL, status TEXT NOT NULL, applied_ts REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS impressions (ts REAL, env TEXT, flag TEXT, variation INTEGER, user_key TEXT);
CREATE INDEX IF NOT EXISTS imp_flag ON impressions (env, flag, ts);
CREATE TABLE IF NOT EXISTS conversions (ts REAL, env TEXT, metric TEXT, user_key TEXT, value REAL);
CREATE INDEX IF NOT EXISTS conv_metric ON conversions (env, metric);
CREATE TABLE IF NOT EXISTS eval_stats (
  env TEXT, flag TEXT, count INTEGER NOT NULL, first_ts REAL, last_ts REAL, PRIMARY KEY (env, flag)
);
"""

CRITICAL_TAG = "critical"
APPROVAL_ENV = "prod"


class StoreError(Exception):
    status = 400

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


class NotFound(StoreError):
    status = 404


class Conflict(StoreError):
    status = 409


class Forbidden(StoreError):
    status = 403


def _env_view(doc: dict | None, env: str) -> dict | None:
    if doc is None:
        return None
    rs = engine.ruleset_for_env([doc], [], env)
    view = rs["flags"].get(doc["key"])
    if view:
        view.pop("version", None)
    return view


def _only_turns_off(before: dict | None, after: dict | None) -> bool:
    """A change that only turns a flag off (or kills it) never waits for approval: off is the safe side."""
    if before is None or after is None:
        return False
    b, a = dict(before), dict(after)
    turned_off = (b["on"] and not a["on"]) or (not b["killed"] and a["killed"])
    for k in ("on", "killed"):
        b.pop(k)
        a.pop(k)
    return turned_off and a == b


class Store:
    def __init__(self, path: str = ":memory:", envs: list[str] | None = None, clock: Callable[[], float] = time.time):
        self.envs = envs or ["dev", "staging", "prod"]
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('rev', 0)")
        self.db.commit()
        self.listeners: list[Callable[[list[str]], None]] = []

    # ---------- reads ----------

    def revision(self) -> int:
        return self.db.execute("SELECT v FROM meta WHERE k='rev'").fetchone()[0]

    def list_flags(self) -> list[dict]:
        with self.lock:
            return [json.loads(r["doc"]) for r in self.db.execute("SELECT doc FROM flags ORDER BY key")]

    def get_flag(self, key: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT doc FROM flags WHERE key=?", (key,)).fetchone()
        if row is None:
            raise NotFound(f"flag {key!r} not found")
        return json.loads(row["doc"])

    def list_segments(self) -> list[dict]:
        with self.lock:
            return [json.loads(r["doc"]) for r in self.db.execute("SELECT doc FROM segments ORDER BY key")]

    def get_segment(self, key: str) -> dict:
        row = self.db.execute("SELECT doc FROM segments WHERE key=?", (key,)).fetchone()
        if row is None:
            raise NotFound(f"segment {key!r} not found")
        return json.loads(row["doc"])

    def ruleset(self, env: str) -> dict:
        with self.lock:
            return engine.ruleset_for_env(self.list_flags(), self.list_segments(), env, self.revision())

    def audit(self, entity_key: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM audit", []
        if entity_key:
            q, args = q + " WHERE entity_key=?", [entity_key]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("before", "after", "diff"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    # ---------- write helpers ----------

    def _bump_rev(self) -> int:
        self.db.execute("UPDATE meta SET v = v + 1 WHERE k='rev'")
        return self.revision()

    def _audit(self, actor, action, etype, key, before, after, note=None):
        d = diff(before, after)
        self.db.execute(
            "INSERT INTO audit (ts, actor, action, entity_type, entity_key, before, after, diff, note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.clock(),
                actor,
                action,
                etype,
                key,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
                json.dumps(d),
                note,
            ),
        )

    def _notify(self, envs):
        for fn in self.listeners:
            try:
                fn(envs)
            except Exception:  # noqa: BLE001  a broken listener must not fail a write
                pass

    def _validate(self, doc: dict, all_flags: list[dict]):
        errors = validation.validate_flag(doc, self.envs, {s["key"] for s in self.list_segments()})
        others = [f for f in all_flags if f["key"] != doc["key"]] + [doc]
        known = {f["key"] for f in others}
        for env in self.envs:
            for p in doc.get("environments", {}).get(env, {}).get("prerequisites", []):
                if p["key"] not in known:
                    errors.append(f"{env}: unknown prerequisite {p['key']!r}")
            cycle = validation.prerequisite_cycle(others, env)
            if cycle:
                errors.append(f"{env}: prerequisite cycle {' -> '.join(cycle)}")
        if errors:
            raise StoreError("validation failed", errors=errors)

    def _commit_flag(self, before: dict | None, after: dict, actor: str, action: str, note=None) -> dict:
        now = self.clock()
        after = copy.deepcopy(after)
        after["version"] = (before["version"] + 1) if before else 1
        after["created_at"] = before["created_at"] if before else now
        after["updated_at"] = now
        if before is None:
            self.db.execute("INSERT INTO flags VALUES (?,?,?)", (after["key"], json.dumps(after), after["version"]))
        else:
            cur = self.db.execute(
                "UPDATE flags SET doc=?, version=? WHERE key=? AND version=?",
                (json.dumps(after), after["version"], after["key"], before["version"]),
            )
            if cur.rowcount != 1:
                raise Conflict("flag changed underneath this write")
        self._audit(actor, action, "flag", after["key"], before, after, note)
        self._bump_rev()
        self.db.commit()
        changed = [e for e in self.envs if _env_view(before, e) != _env_view(after, e)]
        self._notify(changed or self.envs)
        return after

    def needs_approval(self, before: dict | None, after: dict) -> bool:
        tags = set((before or {}).get("tags", [])) | set(after.get("tags", []))
        if CRITICAL_TAG not in tags or before is None:
            return False
        b, a = _env_view(before, APPROVAL_ENV), _env_view(after, APPROVAL_ENV)
        if b == a:
            return False
        return not _only_turns_off(b, a)

    # ---------- flags ----------

    def create_flag(self, doc: dict, actor: str) -> dict:
        with self.lock:
            doc = copy.deepcopy(doc)
            if self.db.execute("SELECT 1 FROM flags WHERE key=?", (doc.get("key"),)).fetchone():
                raise Conflict(f"flag {doc['key']!r} already exists")
            doc.setdefault("salt", secrets.token_hex(4))
            doc.setdefault("tags", [])
            doc.setdefault("killed", False)
            envs = doc.setdefault("environments", {})
            for e in self.envs:
                envs.setdefault(e, {"on": False, "off_variation": len(doc.get("variations", [])) - 1, "rules": []})
                envs[e].setdefault("fallthrough", {"variation": 0})
            for k in ("version", "created_at", "updated_at"):
                doc.pop(k, None)
            self._validate(doc, self.list_flags())
            return self._commit_flag(None, doc, actor, "flag.create")

    def update_flag(self, key: str, doc: dict, expected_version: int, actor: str) -> dict:
        """Returns {"status": "applied", "flag": ...} or {"status": "pending_approval", "change_request": ...}."""
        with self.lock:
            before = self.get_flag(key)
            if before["version"] != expected_version:
                raise Conflict(
                    f"version mismatch: you edited version {expected_version}, current is {before['version']}",
                    current_version=before["version"],
                )
            after = copy.deepcopy(doc)
            after.pop("evaluations", None)  # a read-only field the list endpoint adds
            after["key"] = key
            for k in ("salt", "created_at"):
                after.setdefault(k, before.get(k))
            self._validate(after, self.list_flags())
            meta = ("version", "created_at", "updated_at", "evaluations")
            if not diff(
                {k: v for k, v in before.items() if k not in meta}, {k: v for k, v in after.items() if k not in meta}
            ):
                return {"status": "unchanged", "flag": before}
            if self.needs_approval(before, after):
                cr = self._create_change_request("flag.update", key, before["version"], after, actor)
                return {"status": "pending_approval", "change_request": cr}
            return {"status": "applied", "flag": self._commit_flag(before, after, actor, "flag.update")}

    def set_env_state(self, key: str, env: str, actor: str, on=None, killed=None, expected_version=None) -> dict:
        """Toggle or kill switch for one environment, the fast path from the flag list."""
        with self.lock:
            flag = self.get_flag(key)
            if env not in self.envs:
                raise NotFound(f"environment {env!r} not found")
            doc = copy.deepcopy(flag)
            if on is not None:
                doc["environments"][env]["on"] = bool(on)
            if killed is not None:
                doc["environments"][env]["killed"] = bool(killed)
            return self.update_flag(key, doc, flag["version"] if expected_version is None else expected_version, actor)

    def archive_flag(self, key: str, actor: str) -> None:
        with self.lock:
            before = self.get_flag(key)
            self.db.execute("DELETE FROM flags WHERE key=?", (key,))
            self._audit(actor, "flag.archive", "flag", key, before, None)
            self._bump_rev()
            self.db.commit()
            self._notify(self.envs)

    # ---------- approvals (four eyes for critical flags in prod) ----------

    def _create_change_request(self, kind, key, base_version, payload, actor) -> dict:
        cr_id = "cr_" + secrets.token_hex(4)
        self.db.execute(
            "INSERT INTO change_requests (id, ts, kind, flag_key, base_version, payload, requested_by, status)"
            " VALUES (?,?,?,?,?,?,?, 'pending')",
            (cr_id, self.clock(), kind, key, base_version, json.dumps(payload), actor),
        )
        before = self.get_flag(key) if kind == "flag.update" else {}
        self._audit(actor, "approval.request", "flag", key, before, payload, cr_id)
        self.db.commit()
        return self.get_change_request(cr_id)

    def get_change_request(self, cr_id: str) -> dict:
        row = self.db.execute("SELECT * FROM change_requests WHERE id=?", (cr_id,)).fetchone()
        if row is None:
            raise NotFound(f"change request {cr_id!r} not found")
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def list_change_requests(self, status: str | None = "pending") -> list[dict]:
        q = "SELECT id FROM change_requests" + (" WHERE status=?" if status else "") + " ORDER BY ts DESC"
        return [self.get_change_request(r[0]) for r in self.db.execute(q, (status,) if status else ())]

    def decide(self, cr_id: str, actor: str, approve: bool, note: str | None = None) -> dict:
        with self.lock:
            cr = self.get_change_request(cr_id)
            if cr["status"] != "pending":
                raise Conflict(f"change request is already {cr['status']}")
            if approve and actor == cr["requested_by"]:
                raise Forbidden("four-eyes rule: the requester cannot approve their own change")
            now = self.clock()
            if not approve:
                self.db.execute(
                    "UPDATE change_requests SET status='rejected', decided_by=?, decided_ts=?, note=? WHERE id=?",
                    (actor, now, note, cr_id),
                )
                self._audit(actor, "approval.reject", "flag", cr["flag_key"], None, None, cr_id)
                self.db.commit()
                return self.get_change_request(cr_id)
            current = self.get_flag(cr["flag_key"])
            if cr["kind"] == "flag.update" and current["version"] != cr["base_version"]:
                self.db.execute(
                    "UPDATE change_requests SET status='stale', decided_by=?, decided_ts=? WHERE id=?",
                    (actor, now, cr_id),
                )
                self.db.commit()
                raise Conflict(
                    "flag changed since this request was made; ask for a new one", current_version=current["version"]
                )
            self.db.execute(
                "UPDATE change_requests SET status='approved', decided_by=?, decided_ts=?, note=? WHERE id=?",
                (actor, now, note, cr_id),
            )
            self._audit(actor, "approval.approve", "flag", cr["flag_key"], None, None, cr_id)
            if cr["kind"] == "flag.update":
                self._commit_flag(
                    current, cr["payload"], cr["requested_by"], "flag.update", f"approved by {actor} ({cr_id})"
                )
            elif cr["kind"] == "schedule.create":
                self.db.execute("UPDATE schedules SET status='pending' WHERE id=?", (cr["payload"]["schedule_id"],))
                self.db.commit()
            return self.get_change_request(cr_id)

    # ---------- scheduled changes ----------

    def create_schedule(self, key: str, env: str, run_at: float, action: dict, actor: str) -> dict:
        with self.lock:
            flag = self.get_flag(key)
            if env not in self.envs:
                raise NotFound(f"environment {env!r} not found")
            if not ({"on", "rollout_percent", "killed"} & set(action)):
                raise StoreError("action needs one of on, killed, rollout_percent")
            if "rollout_percent" in action:
                p = action["rollout_percent"]
                if flag["kind"] != "boolean" or not isinstance(p, int | float) or not 0 <= p <= 100:
                    raise StoreError("rollout_percent is 0 to 100 and only for boolean flags")
            sid = "sch_" + secrets.token_hex(4)
            gated = CRITICAL_TAG in flag.get("tags", []) and env == APPROVAL_ENV and not _turns_off(action)
            status = "awaiting_approval" if gated else "pending"
            self.db.execute(
                "INSERT INTO schedules (id, flag_key, env, run_at, action, created_by, status) VALUES (?,?,?,?,?,?,?)",
                (sid, key, env, run_at, json.dumps(action), actor, status),
            )
            self._audit(
                actor, "schedule.create", "flag", key, {}, {"env": env, "run_at": run_at, "action": action}, sid
            )
            self.db.commit()
            sched = self.get_schedule(sid)
            if gated:
                sched["change_request"] = self._create_change_request(
                    "schedule.create", key, None, {"schedule_id": sid, "env": env, "action": action}, actor
                )
            return sched

    def get_schedule(self, sid: str) -> dict:
        row = self.db.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise NotFound(f"schedule {sid!r} not found")
        d = dict(row)
        d["action"] = json.loads(d["action"])
        return d

    def list_schedules(self, key: str | None = None) -> list[dict]:
        q = "SELECT id FROM schedules" + (" WHERE flag_key=?" if key else "") + " ORDER BY run_at"
        return [self.get_schedule(r[0]) for r in self.db.execute(q, (key,) if key else ())]

    def apply_due_schedules(self) -> list[dict]:
        """Apply every pending schedule whose time has come, oldest first. Called by a background loop."""
        applied = []
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM schedules WHERE status='pending' AND run_at <= ? ORDER BY run_at", (self.clock(),)
            ).fetchall()
            for (sid,) in rows:
                s = self.get_schedule(sid)
                try:
                    before = self.get_flag(s["flag_key"])
                    after = apply_action(before, s["env"], s["action"])
                    self._validate(after, self.list_flags())
                    self._commit_flag(before, after, f"scheduler ({s['created_by']})", "schedule.apply", sid)
                    self.db.execute(
                        "UPDATE schedules SET status='applied', applied_ts=? WHERE id=?", (self.clock(), sid)
                    )
                except StoreError as e:
                    self.db.execute("UPDATE schedules SET status='failed', error=? WHERE id=?", (str(e), sid))
                self.db.commit()
                applied.append(self.get_schedule(sid))
        return applied

    # ---------- segments ----------

    def put_segment(self, seg: dict, actor: str, expected_version: int | None = None) -> dict:
        with self.lock:
            errors = validation.validate_segment(seg)
            if errors:
                raise StoreError("validation failed", errors=errors)
            row = self.db.execute("SELECT doc FROM segments WHERE key=?", (seg["key"],)).fetchone()
            before = json.loads(row["doc"]) if row else None
            if before and expected_version != before["version"]:
                raise Conflict(
                    f"version mismatch: you edited version {expected_version}, current is {before['version']}",
                    current_version=before["version"],
                )
            after = {**seg, "version": (before["version"] + 1) if before else 1}
            self.db.execute(
                "INSERT OR REPLACE INTO segments VALUES (?,?,?)", (seg["key"], json.dumps(after), after["version"])
            )
            self._audit(actor, "segment.update" if before else "segment.create", "segment", seg["key"], before, after)
            self._bump_rev()
            self.db.commit()
            self._notify(self.envs)
            return after

    # ---------- events ----------

    def record_events(self, env: str, events: list[dict]) -> int:
        n = 0
        with self.lock:
            for e in events:
                ts = float(e.get("ts", self.clock()))
                if e.get("type") == "impression":
                    self.db.execute(
                        "INSERT INTO impressions VALUES (?,?,?,?,?)",
                        (ts, env, e["flag"], e.get("variation"), e["user_key"]),
                    )
                    self.db.execute(
                        "INSERT INTO eval_stats VALUES (?,?,?,?,?) ON CONFLICT(env, flag) DO UPDATE SET"
                        " count = count + excluded.count, last_ts = MAX(last_ts, excluded.last_ts)",
                        (env, e["flag"], int(e.get("count", 1)), ts, ts),
                    )
                    n += 1
                elif e.get("type") == "conversion":
                    self.db.execute(
                        "INSERT INTO conversions VALUES (?,?,?,?,?)",
                        (ts, env, e["metric"], e["user_key"], float(e.get("value", 1.0))),
                    )
                    n += 1
            self.db.commit()
        return n

    def eval_stats(self) -> dict:
        out: dict = {}
        for r in self.db.execute("SELECT flag, SUM(count) c, MAX(last_ts) l FROM eval_stats GROUP BY flag"):
            out[r["flag"]] = {"count": r["c"], "last_ts": r["l"]}
        return out

    def export(self) -> dict:
        return {"generated_at": self.clock(), "flags": self.list_flags(), "eval_stats": self.eval_stats()}

    def experiment_data(self, key: str, env: str) -> dict:
        """First exposure per user, then whether that user converted on the primary metric after it."""
        flag = self.get_flag(key)
        exp = flag.get("experiment")
        if not exp:
            raise StoreError(f"flag {key!r} is not an experiment")
        first: dict[str, tuple[float, int]] = {}
        multi = set()
        for ts, user, var in self.db.execute(
            "SELECT ts, user_key, variation FROM impressions WHERE env=? AND flag=? ORDER BY ts", (env, key)
        ):
            if var is None:
                continue
            if user not in first:
                first[user] = (ts, var)
            elif first[user][1] != var:
                multi.add(user)
        converted = {}
        for ts, user in self.db.execute(
            "SELECT ts, user_key FROM conversions WHERE env=? AND metric=? ORDER BY ts", (env, exp["primary_metric"])
        ):
            if user in first and ts >= first[user][0] and user not in converted:
                converted[user] = True
        n = len(flag["variations"])
        exposures, conversions = [0] * n, [0] * n
        for user, (_, var) in first.items():
            exposures[var] += 1
            conversions[var] += 1 if user in converted else 0
        cfg = flag["environments"][env]
        rollout = cfg.get("fallthrough", {}).get("rollout")
        weights = exp.get("weights") or (rollout["weights"] if rollout else [1] * n)
        return {
            "flag": flag,
            "exposures": exposures,
            "conversions": conversions,
            "weights": weights,
            "users_in_multiple_variations": len(multi),
        }


def _turns_off(action: dict) -> bool:
    return action.get("on") is False or action.get("killed") is True


def apply_action(flag: dict, env: str, action: dict) -> dict:
    doc = copy.deepcopy(flag)
    cfg = doc["environments"][env]
    if "on" in action:
        cfg["on"] = bool(action["on"])
    if "killed" in action:
        cfg["killed"] = bool(action["killed"])
    if "rollout_percent" in action:
        true_idx = next(i for i, v in enumerate(doc["variations"]) if v["value"] is True)
        w = [0] * len(doc["variations"])
        w[true_idx] = round(action["rollout_percent"] * 1000)
        w[1 - true_idx] = engine.BUCKETS - w[true_idx]
        cfg["fallthrough"] = {"rollout": {"weights": w, "bucket_by": "key"}}
    return doc
