"""Unified activity log for the Hermes container.

Every subsystem (MCP subprocesses, genui mutations, gbrain dispatch) appends
one JSON line per event to /data/.hermes/activity/activity-YYYY-Www.jsonl.
The /ui/activity dashboard reads the current + previous week and renders
NOW (active jobs) / CAN (skills + usage) / DID (this feed).

Why JSONL + weekly rotation:
  - Append-only, no locking required (every write is one line under
    PIPE_BUF on Linux, so concurrent writers don't tear).
  - Weekly rotation caps file size and lets old data age out without a
    janitor process — readers just stop looking past 2 weeks back.
  - Plain text, debuggable with `tail -f`, no DB needed.

Schema (one event per line):
  {
    "ts": "2026-05-13T14:02:31Z",          # ISO 8601 UTC
    "kind": "tool_call" | "view_mutation" | "artifact" | "job",
    "source": "mcp_gbrain" | "mcp_genui" | "mcp_constellation" | "genui" | "gbrain",
    "name": "<tool name | mutation name>", # e.g. "search", "add_item"
    "summary": "...",                      # optional human-readable detail
    "outcome": "ok" | "error",             # missing means "in_flight" — most calls
                                           # write only the outcome line; this field
                                           # exists for future correlation work.
    "latency_ms": 123,                     # optional, when timing was captured
    "error": "..."                         # optional, short message on failure
  }

Reader returns dicts in newest-first order; truncates malformed lines silently.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


ACTIVITY_DIR = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "activity"


def _ensure_dir() -> None:
    try:
        ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _week_filename(when: datetime | None = None) -> str:
    """ISO-week-stamped filename. Same scheme as the friction log so an
    operator who already knows the audit naming finds these immediately."""
    when = when or datetime.now(timezone.utc)
    year, week, _ = when.isocalendar()
    return f"activity-{year}-W{week:02d}.jsonl"


def _current_path() -> Path:
    return ACTIVITY_DIR / _week_filename()


def append(
    *,
    kind: str,
    source: str,
    name: str,
    summary: str | None = None,
    outcome: str | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Write one event. Best-effort — any IO failure is swallowed so
    instrumentation can never break the calling subsystem."""
    _ensure_dir()
    rec: dict = {
        "ts": _now_iso(),
        "kind": kind,
        "source": source,
        "name": name,
    }
    if summary is not None:
        rec["summary"] = summary
    if outcome is not None:
        rec["outcome"] = outcome
    if latency_ms is not None:
        rec["latency_ms"] = latency_ms
    if error is not None:
        # Truncate aggressively — the activity log is a feed, not a
        # diagnostic dump. Real errors stay in the per-subsystem stderr.
        rec["error"] = error[:200]
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with open(_current_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _iter_recent_files(weeks_back: int = 2) -> Iterable[Path]:
    """Yield the current week file + previous N-1 weeks, newest first."""
    now = datetime.now(timezone.utc)
    for delta in range(weeks_back):
        yield ACTIVITY_DIR / _week_filename(now - timedelta(weeks=delta))


def read_recent(*, limit: int = 100, weeks_back: int = 2) -> list[dict]:
    """Return up to `limit` events from the last `weeks_back` weeks,
    newest first. Drops malformed lines silently."""
    _ensure_dir()
    events: list[dict] = []
    for path in _iter_recent_files(weeks_back=weeks_back):
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]


def tool_usage_counts(*, days: int = 7) -> dict[str, int]:
    """Return tool-name → call-count for the last N days. Used by the
    CAN panel to rank skills by their declared-tools' aggregate usage.

    A tool is identified by `f"{source}.{name}"` — e.g. `mcp_gbrain.search`,
    `mcp_genui.add_item` — so two MCPs that happen to share a verb don't
    collide in the count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    counts: dict[str, int] = {}
    # Look 2 weeks back so a 7-day window across a week boundary works.
    for ev in read_recent(limit=10_000, weeks_back=2):
        if ev.get("kind") != "tool_call":
            continue
        if (ev.get("ts") or "") < cutoff_iso:
            continue
        if ev.get("outcome") == "error":
            # Counting errors as usage rewards broken flows; skip them.
            continue
        key = f"{ev.get('source', '')}.{ev.get('name', '')}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def last_used_map(*, weeks_back: int = 4) -> dict[str, str]:
    """Return tool-name → most-recent ISO ts. Lets the CAN panel show
    'last used 2h ago' next to each skill."""
    out: dict[str, str] = {}
    for ev in read_recent(limit=50_000, weeks_back=weeks_back):
        if ev.get("kind") != "tool_call":
            continue
        key = f"{ev.get('source', '')}.{ev.get('name', '')}"
        # read_recent is sorted newest-first, so the first hit per key wins.
        if key not in out:
            out[key] = ev.get("ts", "")
    return out
