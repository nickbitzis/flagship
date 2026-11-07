"""Code reference scanner: find flag keys in a codebase and say which flags are safe to remove.

  flagship scan path/ --flags export.json --days 30 [--markdown]

Buckets in the report:
  remove_flag   stale and no code references left: archive it on the server
  clean_up      stale but still referenced: inline the value at these lines, then remove the flag
  unreferenced  not stale yet, but no code references (maybe used by another repo)
  missing       keys passed to an SDK call in code that the server does not know (always the default)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .stale import find_stale

EXTENSIONS = {
    ".py",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".java",
    ".kt",
    ".rb",
    ".swift",
    ".html",
    ".yml",
    ".yaml",
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".ruff_cache", ".pytest_cache"}
SDK_CALL = re.compile(
    r"""(?:variation|variation_detail|is_enabled|bool_variation|flag)\(\s*["']([a-z0-9][a-z0-9._-]+)["']"""
)


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in EXTENSIONS:
                yield p


def find_references(root: Path, keys: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    """Return ({key: ["file:line", ...]}, keys passed to SDK calls in code)."""
    patterns = {k: re.compile(r"""["'`]""" + re.escape(k) + r"""["'`]""") for k in keys}
    refs: dict[str, list[str]] = {k: [] for k in keys}
    called: set[str] = set()
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root) if root.is_dir() else path.name
        for lineno, line in enumerate(text.splitlines(), 1):
            called.update(SDK_CALL.findall(line))
            for k, pat in patterns.items():
                if pat.search(line):
                    refs[k].append(f"{rel}:{lineno}")
    return refs, called


def scan(root: Path, export: dict, now: float, days: int = 30, env: str = "prod") -> dict:
    keys = [f["key"] for f in export.get("flags", [])]
    refs, called = find_references(root, keys)
    stale = {s["key"]: s for s in find_stale(export, now, days, env)}
    report = {"remove_flag": [], "clean_up": [], "unreferenced": [], "missing": sorted(called - set(keys))}
    for k in keys:
        if k in stale:
            entry = {**stale[k], "references": refs[k]}
            report["clean_up" if refs[k] else "remove_flag"].append(entry)
        elif not refs[k]:
            report["unreferenced"].append({"key": k, "references": []})
    report["summary"] = {
        "flags": len(keys),
        "files_scanned": sum(1 for _ in iter_files(root)),
        "stale": len(stale),
        "safe_to_remove": len(report["remove_flag"]),
    }
    return report


def to_markdown(report: dict, days: int) -> str:
    s = report["summary"]
    lines = [
        "## Flagship stale flag report",
        "",
        f"{s['flags']} flags, {s['files_scanned']} files scanned, {s['stale']} stale (constant or unused "
        f"for more than {days} days), {s['safe_to_remove']} safe to remove now.",
        "",
    ]

    def table(title, rows, with_refs):
        if not rows:
            return
        lines.extend([f"### {title}", "", "| Flag | Why | Days | References |", "|---|---|---|---|"])
        for r in rows:
            where = ", ".join(f"`{x}`" for x in r["references"][:5]) if with_refs else "none"
            lines.append(f"| `{r['key']}` | {r.get('detail', 'no code references')} | {r.get('days', '')} | {where} |")
        lines.append("")

    table("Safe to remove (stale, no code references)", report["remove_flag"], False)
    table("Clean up code first (stale, still referenced)", report["clean_up"], True)
    table("No code references in this repo", report["unreferenced"], False)
    if report["missing"]:
        lines.extend(["### Referenced in code but not defined on the server", ""])
        lines.extend(f"- `{k}`" for k in report["missing"])
        lines.append("")
    return "\n".join(lines)


def to_text(report: dict) -> str:
    out = []
    for bucket in ("remove_flag", "clean_up", "unreferenced"):
        for r in report[bucket]:
            refs = ", ".join(r["references"][:3]) or "-"
            out.append(f"{bucket:<13} {r['key']:<28} {r.get('detail', ''):<50} {refs}")
    for k in report["missing"]:
        out.append(f"{'missing':<13} {k:<28} referenced in code, not on the server")
    s = report["summary"]
    out.append(
        f"\n{s['flags']} flags, {s['files_scanned']} files, {s['stale']} stale, {s['safe_to_remove']} safe to remove"
    )
    return "\n".join(out)
