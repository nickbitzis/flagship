"""Prometheus text exposition without a client library."""

from __future__ import annotations

import threading
from collections import defaultdict

LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)


def _labels(d: dict) -> str:
    if not d:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}"


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = defaultdict(int)  # (route, method, status)
        self.flag_changes = defaultdict(int)  # env
        self.events = defaultdict(int)  # (env, type)
        self.evaluations = defaultdict(int)  # env, server-side evaluations
        self.sse_clients = defaultdict(int)  # env
        self.sse_pushes = 0
        self.schedules_applied = 0
        self.buckets = [0] * len(LATENCY_BUCKETS)
        self.latency_sum = 0.0
        self.latency_count = 0

    def observe_request(self, route: str, method: str, status: int, seconds: float):
        with self.lock:
            self.requests[(route, method, str(status))] += 1
            self.latency_sum += seconds
            self.latency_count += 1
            for i, b in enumerate(LATENCY_BUCKETS):
                if seconds <= b:
                    self.buckets[i] += 1

    def inc(self, name: str, key=None, n: int = 1):
        with self.lock:
            attr = getattr(self, name)
            if isinstance(attr, int):
                setattr(self, name, attr + n)
            else:
                attr[key] += n

    def render(self, revision: int, flags: int) -> str:
        lines = []

        def family(name, kind, help_text, samples):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                lines.append(f"{name}{_labels(labels)} {value}")

        with self.lock:
            family(
                "flagship_http_requests_total",
                "counter",
                "HTTP requests by route and status.",
                [({"route": r, "method": m, "status": s}, v) for (r, m, s), v in sorted(self.requests.items())],
            )
            # self.buckets is already cumulative: observe_request counts every bucket at or above the latency.
            samples = [({"le": str(b)}, c) for b, c in zip(LATENCY_BUCKETS, self.buckets, strict=True)]
            samples.append(({"le": "+Inf"}, self.latency_count))
            lines.append("# HELP flagship_http_request_seconds Request latency.")
            lines.append("# TYPE flagship_http_request_seconds histogram")
            for labels, v in samples:
                lines.append(f"flagship_http_request_seconds_bucket{_labels(labels)} {v}")
            lines.append(f"flagship_http_request_seconds_sum {self.latency_sum:.6f}")
            lines.append(f"flagship_http_request_seconds_count {self.latency_count}")
            family(
                "flagship_flag_changes_total",
                "counter",
                "Committed flag changes per environment.",
                [({"env": e}, v) for e, v in sorted(self.flag_changes.items())],
            )
            family(
                "flagship_events_ingested_total",
                "counter",
                "Impressions and conversions received from SDKs.",
                [({"env": e, "type": t}, v) for (e, t), v in sorted(self.events.items())],
            )
            family(
                "flagship_server_evaluations_total",
                "counter",
                "Evaluations done on the server (client keys and the UI).",
                [({"env": e}, v) for e, v in sorted(self.evaluations.items())],
            )
            family(
                "flagship_sse_clients",
                "gauge",
                "Connected streaming SDKs.",
                [({"env": e}, v) for e, v in sorted(self.sse_clients.items())],
            )
            family(
                "flagship_sse_pushes_total",
                "counter",
                "Ruleset pushes sent to streaming SDKs.",
                [({}, self.sse_pushes)],
            )
            family(
                "flagship_schedules_applied_total",
                "counter",
                "Scheduled changes applied.",
                [({}, self.schedules_applied)],
            )
            family("flagship_ruleset_revision", "gauge", "Current ruleset revision.", [({}, revision)])
            family("flagship_flags", "gauge", "Number of flags.", [({}, flags)])
        return "\n".join(lines) + "\n"
