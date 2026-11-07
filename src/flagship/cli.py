"""flagship serve | scan | bench | export

flagship serve                         server and web UI on http://127.0.0.1:8788
flagship scan PATH --flags FILE        stale flag report for a codebase (or --server URL)
flagship bench                         evaluations per second on the demo ruleset
flagship export --out FILE             write the demo flag export used by scan in CI
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def cmd_serve(a):
    import uvicorn

    uvicorn.run(
        "flagship.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", a.port)),
        access_log=False,
        timeout_graceful_shutdown=2,
    )


def _load_export(a) -> dict:
    if a.flags:
        return json.loads(Path(a.flags).read_text())
    import httpx

    return httpx.get(f"{a.server.rstrip('/')}/api/export", timeout=10).json()


def cmd_scan(a):
    from .scan import scan, to_markdown, to_text

    export = _load_export(a)
    now = a.now if a.now is not None else export.get("generated_at", time.time())
    report = scan(Path(a.path), export, now, a.days, a.env)
    if a.format == "json":
        print(json.dumps(report, indent=2, default=str))
    elif a.format == "markdown":
        print(to_markdown(report, a.days))
    else:
        print(to_text(report))
    if a.fail_on_stale and report["summary"]["safe_to_remove"]:
        sys.exit(1)


def cmd_bench(a):
    import random

    from . import engine
    from .seed import seed, synthetic_user
    from .store import Store

    s = Store(":memory:")
    seed(s, simulate=False)
    rs = s.ruleset("prod")
    rng = random.Random(1)
    users = [synthetic_user(rng, i) for i in range(1000)]
    keys = list(rs["flags"])
    n = a.n
    t = time.perf_counter()
    for i in range(n):
        engine.evaluate(rs, keys[i % len(keys)], users[i % len(users)])
    elapsed = time.perf_counter() - t
    print(f"{n} evaluations over {len(keys)} flags in {elapsed:.2f}s: {n / elapsed:,.0f} evaluations/second")
    print(f"mean {elapsed / n * 1e6:.1f} microseconds per evaluation (one core, Python {sys.version.split()[0]})")


def cmd_export(a):
    from .seed import seed
    from .store import Store

    s = Store(":memory:")
    seed(s)
    Path(a.out).write_text(json.dumps(s.export(), indent=1) + "\n")
    print(f"wrote {a.out}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="flagship")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8788)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("scan", help="find flag references in code and report flags safe to remove")
    s.add_argument("path")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--flags", help="flag export JSON (GET /api/export)")
    g.add_argument("--server", help="Flagship base URL")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--env", default="prod")
    s.add_argument("--now", type=float, default=None, help="epoch seconds, defaults to the export time")
    s.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    s.add_argument("--fail-on-stale", action="store_true")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("bench")
    s.add_argument("-n", type=int, default=200_000)
    s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("export")
    s.add_argument("--out", default="examples/flags-export.json")
    s.set_defaults(fn=cmd_export)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
