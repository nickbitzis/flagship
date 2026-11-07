"""Python server-side SDK.

    client = FlagshipClient("http://127.0.0.1:8788", "sdk-prod-demo", cache_path=".flagship-cache.json")
    if client.variation("new-checkout", {"key": "user-123", "country": "US"}, default=False):
        ...

- Fetches the ruleset once at start, then evaluates locally with the shared engine. No network call
  per evaluation.
- Subscribes to /sdk/stream (Server-Sent Events). Every frame is a full ruleset. On a dropped
  connection it reconnects with exponential backoff and full jitter, and the first frame after a
  reconnect resyncs anything it missed.
- Fallback order when the server is down: the ruleset in memory, then the last known good ruleset
  on disk (cache_path), then the default passed in code.
- Impressions are summarized per (flag, user, variation) and sent in batches by a background
  thread. If a flush fails the events go back on a bounded queue (oldest dropped first).
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

import httpx

from . import engine


class FlagshipClient:
    def __init__(
        self,
        base_url: str,
        sdk_key: str,
        *,
        cache_path: str | os.PathLike | None = None,
        stream: bool = True,
        send_events: bool = True,
        flush_interval: float = 2.0,
        max_queue: int = 10_000,
        timeout: float = 2.0,
        backoff_initial: float = 0.25,
        backoff_max: float = 30.0,
        rng: random.Random | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"authorization": sdk_key}
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.max_queue = max_queue
        self.flush_interval = flush_interval
        self.rng = rng or random.Random()
        self.http = httpx.Client(timeout=timeout)
        self._lock = threading.Lock()
        self._ruleset: dict | None = None
        self.source = "defaults"  # server | stream | cache | defaults
        self.connected = False
        self.reconnects = 0
        self.dropped_events = 0
        self.sent_events = 0
        self._impressions: OrderedDict = OrderedDict()  # (flag, user, variation) -> count
        self._other_events: list[dict] = []
        self._stop = threading.Event()
        self._listeners: list[Callable[[dict], None]] = []
        self._stream_resp = None
        self._bootstrap()
        self._threads = []
        if stream:
            self._threads.append(threading.Thread(target=self._stream_loop, name="flagship-sse", daemon=True))
        if send_events:
            self._threads.append(threading.Thread(target=self._flush_loop, name="flagship-events", daemon=True))
        for t in self._threads:
            t.start()

    # ---------- ruleset ----------

    def _bootstrap(self):
        try:
            r = self.http.get(f"{self.base_url}/sdk/ruleset", headers=self.headers)
            r.raise_for_status()
            self._set_ruleset(r.json(), "server")
            return
        except (httpx.HTTPError, ValueError):
            pass
        cached = self._load_cache()
        if cached is not None:
            self._set_ruleset(cached, "cache", persist=False)

    def _set_ruleset(self, rs: dict, source: str, persist: bool = True):
        with self._lock:
            self._ruleset = rs
            self.source = source
        if persist and self.cache_path:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rs))
            os.replace(tmp, self.cache_path)  # atomic, a crash never leaves half a file
        for fn in self._listeners:
            fn(rs)

    def _load_cache(self) -> dict | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return None

    @property
    def revision(self) -> int | None:
        return self._ruleset["version"] if self._ruleset else None

    def on_change(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _stream_loop(self):
        delay = self.backoff_initial
        first = True
        while not self._stop.is_set():
            if not first:
                # Full jitter: sleep a random time up to the current cap, so a fleet of SDKs
                # does not reconnect in lockstep after a server restart.
                self._stop.wait(self.rng.uniform(0, delay))
                delay = min(self.backoff_max, delay * 2)
                self.reconnects += 1
            first = False
            try:
                timeout = httpx.Timeout(self.timeout, read=45.0)
                with self.http.stream("GET", f"{self.base_url}/sdk/stream", headers=self.headers, timeout=timeout) as r:
                    r.raise_for_status()
                    self._stream_resp = r
                    event, data = None, []
                    for line in r.iter_lines():
                        if self._stop.is_set():
                            return
                        if line == "":
                            if event == "ruleset" and data:
                                self._set_ruleset(json.loads("\n".join(data)), "stream")
                                self.connected = True
                                delay = self.backoff_initial
                            event, data = None, []
                        elif line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
            except (httpx.HTTPError, ValueError, RuntimeError):
                pass
            finally:
                self._stream_resp = None
            self.connected = False

    # ---------- evaluation ----------

    def variation_detail(self, key: str, context: dict, default=None) -> engine.Detail:
        rs = self._ruleset
        if rs is None:
            return engine.Detail(default, None, {"kind": "ERROR", "error_kind": "CLIENT_NOT_READY"})
        d = engine.evaluate(rs, key, context, default)
        user = context.get("key") if isinstance(context, dict) else None
        if user and d.variation is not None:
            self._impression(key, user, d.variation)
            for pk, pd in d.prereq_events:
                if pd.variation is not None:
                    self._impression(pk, user, pd.variation)
        return d

    def variation(self, key: str, context: dict, default=None):
        return self.variation_detail(key, context, default).value

    def is_enabled(self, key: str, context: dict, default: bool = False) -> bool:
        return bool(self.variation(key, context, default))

    # ---------- events ----------

    def _impression(self, flag, user, variation):
        with self._lock:
            k = (flag, user, variation)
            if k in self._impressions:
                self._impressions[k][1] += 1
            else:
                self._impressions[k] = [time.time(), 1]
                if len(self._impressions) + len(self._other_events) > self.max_queue:
                    self._impressions.popitem(last=False)
                    self.dropped_events += 1

    def track(self, metric: str, context: dict, value: float = 1.0) -> None:
        with self._lock:
            self._other_events.append(
                {"type": "conversion", "metric": metric, "user_key": context["key"], "value": value, "ts": time.time()}
            )
            if len(self._impressions) + len(self._other_events) > self.max_queue:
                self._other_events.pop(0)
                self.dropped_events += 1

    def pending_events(self) -> int:
        with self._lock:
            return len(self._impressions) + len(self._other_events)

    def flush(self) -> bool:
        with self._lock:
            imps, others = self._impressions, self._other_events
            self._impressions, self._other_events = OrderedDict(), []
        batch = [
            {"type": "impression", "flag": f, "user_key": u, "variation": v, "ts": ts, "count": c}
            for (f, u, v), (ts, c) in imps.items()
        ] + others
        if not batch:
            return True
        try:
            r = self.http.post(f"{self.base_url}/sdk/events", json={"events": batch}, headers=self.headers)
            r.raise_for_status()
            self.sent_events += len(batch)
            return True
        except httpx.HTTPError:
            with self._lock:  # put them back in front of anything newer, bounded
                merged = OrderedDict(imps)
                for k, v in self._impressions.items():
                    merged[k] = v
                self._impressions = merged
                self._other_events = others + self._other_events
                while len(self._impressions) + len(self._other_events) > self.max_queue:
                    if self._impressions:
                        self._impressions.popitem(last=False)
                    else:
                        self._other_events.pop(0)
                    self.dropped_events += 1
            return False

    def _flush_loop(self):
        while not self._stop.wait(self.flush_interval):
            self.flush()

    # ---------- lifecycle ----------

    def status(self) -> dict:
        return {
            "source": self.source,
            "connected": self.connected,
            "revision": self.revision,
            "reconnects": self.reconnects,
            "pending_events": self.pending_events(),
            "dropped_events": self.dropped_events,
        }

    def close(self, flush: bool = True) -> None:
        self._stop.set()
        if flush:
            self.flush()
        resp = self._stream_resp
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001  closing from another thread can race the reader
                pass
        self.http.close()
