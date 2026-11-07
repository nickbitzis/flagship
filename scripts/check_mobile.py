"""Check that no page of the web UI overflows sideways on a 390px wide phone.

Headless Chrome clamps --window-size to a 500px wide viewport, so each page is loaded in a 390px
iframe instead and measured from the parent. Start the server first: make run.

Usage: uv run python scripts/check_mobile.py --port 8788
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import urllib.parse

from screenshot import chrome

ROUTES = ["#/flags", "#/flag/search-v2", "#/flag/new-checkout", "#/audit", "#/approvals", "#/experiments", "#/stale"]

PAGE = """<!doctype html><body style="margin:0">
<iframe id="f" src="{url}" style="width:390px;height:900px;border:0"></iframe>
<pre id="out">pending</pre><script>
setTimeout(() => {{ try {{
  const d = document.getElementById("f").contentDocument;
  const w = d.documentElement.clientWidth;
  const wide = [...d.querySelectorAll("body *")]
    .filter((e) => e.getBoundingClientRect().right > w + 1 && !e.closest(".table-scroll"))
    .slice(0, 5).map((e) => e.tagName + "." + e.className);
  const sw = d.documentElement.scrollWidth;
  document.getElementById("out").textContent = "RESULT " + w + " " + sw + " " + JSON.stringify(wide);
}} catch (err) {{ document.getElementById("out").textContent = "ERR " + err; }} }}, 2500);
</script></body>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    a = ap.parse_args()
    failed = False
    with tempfile.TemporaryDirectory() as profile:
        for route in ROUTES:
            html = PAGE.format(url=f"http://127.0.0.1:{a.port}/{route}")
            args = [
                chrome(),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=390,900",
                "--virtual-time-budget=5000",
                "--disable-web-security",
                "--disable-site-isolation-trials",
                f"--user-data-dir={profile}",
                "--dump-dom",
                "data:text/html," + urllib.parse.quote(html),
            ]
            out = subprocess.run(args, capture_output=True, text=True, timeout=60).stdout
            m = re.search(r"RESULT (\d+) (\d+) (.*?)</pre>", out)
            if not m:
                err = re.search(r'<pre id="out">(.*?)</pre>', out)
                print(f"{route:<22} could not measure: {err[1] if err else 'no output'}")
                failed = True
                continue
            width, scroll, wide = int(m[1]), int(m[2]), m[3].replace("&quot;", '"')
            ok = scroll <= width and wide == "[]"
            failed |= not ok
            print(f"{route:<22} viewport {width}px, content {scroll}px  {'ok' if ok else 'OVERFLOW ' + wide}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
