"""FastAPI server: management API, SDK endpoints, SSE stream, web UI, health and metrics."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import engine, stale, stats
from .logs import get_logger, log_event
from .metrics import Metrics
from .store import Store, StoreError

STATIC = Path(__file__).parent / "static"
ENVS = ["dev", "staging", "prod"]


def default_keys() -> dict[str, tuple[str, str]]:
    """Map API key -> (environment, kind). Demo keys unless FLAGSHIP_KEYS is set.

    FLAGSHIP_KEYS is JSON: {"<key>": ["prod", "server"], ...}. Server keys read the full ruleset and
    stream it; client keys can only ask for evaluated values of flags marked client_side.
    """
    raw = os.environ.get("FLAGSHIP_KEYS")
    if raw:
        return {k: tuple(v) for k, v in json.loads(raw).items()}
    keys = {}
    for env in ENVS:
        keys[f"sdk-{env}-demo"] = (env, "server")
        keys[f"client-{env}-demo"] = (env, "client")
    return keys


class Hub:
    """Fan-out of ruleset changes to SSE subscribers. Store writes happen on worker threads, so
    notifications cross into the event loop with call_soon_threadsafe."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: dict[str, set[asyncio.Queue]] = {e: set() for e in ENVS}

    def notify(self, envs: list[str]):
        if self.loop is None:
            return
        for env in envs:
            for q in list(self.subs.get(env, ())):
                self.loop.call_soon_threadsafe(q.put_nowait, "changed")

    def close_all(self):
        if self.loop is None:
            return
        for qs in self.subs.values():
            for q in list(qs):
                self.loop.call_soon_threadsafe(q.put_nowait, None)


def create_app(
    store: Store | None = None, keys: dict | None = None, scheduler_interval: float | None = None
) -> FastAPI:
    db_path = os.environ.get("FLAGSHIP_DB", "data/flagship.db")
    if store is None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        store = Store(db_path, ENVS)
        if not store.list_flags() and os.environ.get("FLAGSHIP_SEED", "1") == "1":
            from .seed import seed

            seed(store)
    keys = keys if keys is not None else default_keys()
    admin_token = os.environ.get("FLAGSHIP_ADMIN_TOKEN")
    interval = (
        scheduler_interval if scheduler_interval is not None else float(os.environ.get("FLAGSHIP_SCHEDULER_S", "5"))
    )
    metrics = Metrics()
    hub = Hub()
    log = get_logger()
    store.listeners.append(hub.notify)
    store.listeners.append(lambda envs: [metrics.inc("flag_changes", e) for e in envs])

    async def scheduler():
        while True:
            await asyncio.sleep(interval)
            try:
                done = await asyncio.to_thread(store.apply_due_schedules)
                for s in done:
                    metrics.inc("schedules_applied")
                    log_event(log, "schedule applied", schedule=s["id"], flag=s["flag_key"], status=s["status"])
            except Exception as e:  # noqa: BLE001  keep the loop alive
                log_event(log, "scheduler error", error=str(e))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        hub.loop = asyncio.get_running_loop()
        task = asyncio.create_task(scheduler()) if interval > 0 else None
        log_event(log, "flagship started", flags=len(store.list_flags()), envs=ENVS)
        yield
        hub.close_all()
        if task:
            task.cancel()

    app = FastAPI(title="Flagship", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.hub = hub
    app.state.metrics = metrics

    @app.middleware("http")
    async def observe(request: Request, call_next):
        start = time.perf_counter()
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        elapsed = time.perf_counter() - start
        metrics.observe_request(path, request.method, response.status_code, elapsed)
        response.headers["x-request-id"] = rid
        if not path.startswith("/static") and path not in ("/metrics", "/healthz"):
            log_event(
                log,
                "request",
                rid=rid,
                method=request.method,
                path=path,
                status=response.status_code,
                ms=round(elapsed * 1000, 2),
                actor=request.headers.get("x-actor"),
            )
        return response

    @app.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError):
        return JSONResponse({"error": str(exc), **exc.extra}, status_code=exc.status)

    def actor_of(x_actor: str | None, authorization: str | None) -> str:
        if admin_token and authorization != f"Bearer {admin_token}":
            raise HTTPException(401, "admin token required")
        if not x_actor:
            raise HTTPException(400, "x-actor header is required for changes")
        return x_actor

    def sdk_key(authorization: str | None, kinds=("server",)) -> str:
        entry = keys.get((authorization or "").removeprefix("Bearer ").strip())
        if entry is None:
            raise HTTPException(401, "unknown SDK key")
        env, kind = entry
        if kind not in kinds:
            raise HTTPException(403, f"a {kind} key cannot use this endpoint")
        return env

    # ---------- health, metrics, UI ----------

    @app.get("/healthz")
    def healthz():
        store.revision()
        return {"status": "ok", "revision": store.revision(), "flags": len(store.list_flags())}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        return metrics.render(store.revision(), len(store.list_flags()))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------- management API ----------

    @app.get("/api/environments")
    def environments():
        return {"environments": ENVS, "approval_env": "prod", "critical_tag": "critical"}

    @app.get("/api/flags")
    def list_flags():
        st = store.eval_stats()
        return {"flags": [{**f, "evaluations": st.get(f["key"], {}).get("count", 0)} for f in store.list_flags()]}

    @app.get("/api/flags/{key}")
    def get_flag(key: str):
        return store.get_flag(key)

    @app.post("/api/flags", status_code=201)
    def create_flag(
        doc: Annotated[dict, Body()], x_actor: str | None = Header(None), authorization: str | None = Header(None)
    ):
        return store.create_flag(doc, actor_of(x_actor, authorization))

    @app.put("/api/flags/{key}")
    def update_flag(
        key: str,
        doc: Annotated[dict, Body()],
        x_actor: str | None = Header(None),
        authorization: str | None = Header(None),
    ):
        actor = actor_of(x_actor, authorization)
        if "version" not in doc:
            raise HTTPException(428, "version is required (optimistic concurrency)")
        result = store.update_flag(key, doc, doc["version"], actor)
        return JSONResponse(result, status_code=202 if result["status"] == "pending_approval" else 200)

    @app.post("/api/flags/{key}/environments/{env}")
    def set_env(
        key: str,
        env: str,
        body: Annotated[dict, Body()],
        x_actor: str | None = Header(None),
        authorization: str | None = Header(None),
    ):
        actor = actor_of(x_actor, authorization)
        result = store.set_env_state(key, env, actor, body.get("on"), body.get("killed"), body.get("version"))
        return JSONResponse(result, status_code=202 if result["status"] == "pending_approval" else 200)

    @app.delete("/api/flags/{key}", status_code=204)
    def archive(key: str, x_actor: str | None = Header(None), authorization: str | None = Header(None)):
        store.archive_flag(key, actor_of(x_actor, authorization))

    @app.get("/api/segments")
    def list_segments():
        return {"segments": store.list_segments()}

    @app.put("/api/segments/{key}")
    def put_segment(
        key: str,
        seg: Annotated[dict, Body()],
        x_actor: str | None = Header(None),
        authorization: str | None = Header(None),
    ):
        return store.put_segment({**seg, "key": key}, actor_of(x_actor, authorization), seg.get("version"))

    @app.get("/api/audit")
    def audit(key: str | None = None, limit: int = 100):
        return {"entries": store.audit(key, min(limit, 500))}

    @app.get("/api/approvals")
    def approvals(status: str | None = "pending"):
        return {"change_requests": store.list_change_requests(None if status == "all" else status)}

    @app.post("/api/approvals/{cr_id}/{decision}")
    def decide(
        cr_id: str,
        decision: str,
        body: Annotated[dict | None, Body()] = None,
        x_actor: str | None = Header(None),
        authorization: str | None = Header(None),
    ):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "decision is approve or reject")
        return store.decide(cr_id, actor_of(x_actor, authorization), decision == "approve", (body or {}).get("note"))

    @app.get("/api/schedules")
    def schedules(key: str | None = None):
        return {"schedules": store.list_schedules(key)}

    @app.post("/api/flags/{key}/schedules", status_code=201)
    def create_schedule(
        key: str,
        body: Annotated[dict, Body()],
        x_actor: str | None = Header(None),
        authorization: str | None = Header(None),
    ):
        return store.create_schedule(
            key, body["env"], float(body["run_at"]), body["action"], actor_of(x_actor, authorization)
        )

    @app.post("/api/schedules/run")
    def run_schedules():
        done = store.apply_due_schedules()
        for _ in done:
            metrics.inc("schedules_applied")
        return {"applied": done}

    @app.post("/api/evaluate")
    def evaluate(body: Annotated[dict, Body()]):
        """Evaluate a flag (or an unsaved draft of it) for one user. Powers the editor's live panel."""
        env = body.get("env", "prod")
        if env not in ENVS:
            raise HTTPException(404, "unknown environment")
        flags = store.list_flags()
        draft = body.get("draft")
        key = body.get("flag") or (draft or {}).get("key")
        if draft:
            flags = [f for f in flags if f["key"] != draft["key"]] + [draft]
        rs = engine.ruleset_for_env(flags, store.list_segments(), env, store.revision())
        context = body.get("context") or {}
        d = engine.evaluate(rs, key, context)
        metrics.inc("evaluations", env)
        out = d.to_dict()
        f = rs["flags"].get(key)
        if f and d.variation is not None:
            out["variation_name"] = f["variations"][d.variation].get("name")
        if f and context.get("key"):
            out["bucket"] = engine.bucket(key, f["salt"], str(context["key"])) / 1000
        out["prerequisites"] = [{"key": k, **p.to_dict()} for k, p in d.prereq_events]
        return out

    @app.get("/api/experiments")
    def experiments():
        return {"experiments": [f for f in store.list_flags() if f.get("experiment")]}

    @app.get("/api/experiments/{key}")
    def experiment_readout(key: str, env: str = "prod"):
        data = store.experiment_data(key, env)
        flag = data["flag"]
        r = stats.readout(
            [v.get("name") or str(v["value"]) for v in flag["variations"]],
            data["exposures"],
            data["conversions"],
            data["weights"],
            flag["experiment"].get("control", 0),
        )
        return {
            "key": key,
            "env": env,
            "primary_metric": flag["experiment"]["primary_metric"],
            "hypothesis": flag["experiment"].get("hypothesis"),
            "users_in_multiple_variations": data["users_in_multiple_variations"],
            **r,
        }

    @app.get("/api/stale")
    def stale_flags(days: int = 30, env: str = "prod"):
        return {"days": days, "env": env, "stale": stale.find_stale(store.export(), time.time(), days, env)}

    @app.get("/api/export")
    def export():
        return store.export()

    # ---------- SDK endpoints ----------

    @app.get("/sdk/ruleset")
    def sdk_ruleset(authorization: str | None = Header(None)):
        return store.ruleset(sdk_key(authorization))

    @app.post("/sdk/evaluate")
    def sdk_evaluate(body: Annotated[dict, Body()], authorization: str | None = Header(None)):
        """Client keys (browsers, mobile) never see rules. They send a user and get values back."""
        env = sdk_key(authorization, ("client", "server"))
        rs = store.ruleset(env)
        context = body.get("context") or {}
        out = {}
        for key, f in rs["flags"].items():
            if f.get("client_side"):
                out[key] = engine.evaluate(rs, key, context).to_dict()
        metrics.inc("evaluations", env, len(out))
        return {"env": env, "revision": rs["version"], "flags": out}

    @app.post("/sdk/events", status_code=202)
    def sdk_events(body: Annotated[dict, Body()], authorization: str | None = Header(None)):
        env = sdk_key(authorization, ("client", "server"))
        events = body.get("events", [])
        n = store.record_events(env, events)
        for e in events:
            metrics.inc("events", (env, e.get("type", "unknown")))
        return {"accepted": n}

    @app.get("/sdk/stream")
    async def sdk_stream(request: Request, authorization: str | None = Header(None)):
        env = sdk_key(authorization)
        q: asyncio.Queue = asyncio.Queue()
        hub.subs[env].add(q)
        metrics.inc("sse_clients", env)

        def frame(rs):
            return f"id: {rs['version']}\nevent: ruleset\ndata: {json.dumps(rs, separators=(',', ':'))}\n\n"

        async def gen():
            try:
                yield "retry: 1000\n\n"
                yield frame(await asyncio.to_thread(store.ruleset, env))
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keepalive\n\n"
                        continue
                    if msg is None:
                        break
                    while not q.empty():  # coalesce bursts into one push
                        if q.get_nowait() is None:
                            return
                    metrics.inc("sse_pushes")
                    yield frame(await asyncio.to_thread(store.ruleset, env))
            finally:
                hub.subs[env].discard(q)
                metrics.inc("sse_clients", env, -1)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    return app
