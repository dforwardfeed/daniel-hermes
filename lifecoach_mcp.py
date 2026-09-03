#!/usr/bin/env python3
"""Life Coach MCP server — exposes the user's Life Coach app (daily goals,
gratitude journal, WHOOP/Fitbit fitness data) to Hermes as a stdio MCP server.

Runs as a subprocess spawned by hermes-agent. Line-delimited JSON-RPC 2.0
over stdin/stdout. Logs to stderr. Only dependency: httpx.

Env vars consumed (forwarded by server.py:_build_lifecoach_mcp_entry):
  LIFECOACH_BASE_URL   required — e.g. https://life-coach-assistant.vercel.app
  LIFECOACH_API_TOKEN  required — the app's AGENT_API_TOKEN secret
  LIFECOACH_TIMEOUT    optional — request timeout in seconds (default 30)

Mirrors constellation_mcp.py: same framing, same activity-log hook, same
error posture. Tools auto-namespace as `mcp_lifecoach_<tool>` in Hermes.

Upstream API contract: <BASE_URL>/api/agent/manifest. Key facts:
  - All dates are UTC calendar days. "Today" on the server flips at
    8pm US-Eastern. Tools therefore accept explicit dates; the LLM should
    pass them when the user's local day matters.
  - No endpoint deletes anything. Completing a "general" goal is recorded
    in a sidecar table; the item stays visible in the UI until the user
    checks it off there.
  - Gratitude PUT is an upsert (one entry per day).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


# Force UTF-8 on stdin/stdout/stderr. The MCP wire-format is JSON-RPC over
# stdio and many tool descriptions / responses contain non-ASCII (en-dashes,
# arrows, quotation marks). The Linux default is UTF-8, but Windows defaults
# to cp1252 which raises UnicodeEncodeError on → et al. Reconfiguring at
# startup keeps the code portable; `errors="replace"` ensures one bad byte
# can't take down the whole subprocess. Supported in Python 3.7+.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass  # streams may not be reconfigurable (test harnesses, etc.)


# ──────────────────────────────────────────────────────────────────────────────
# Config (read once at startup; stderr-log on missing values, exit clean).
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("LIFECOACH_BASE_URL", "").strip().rstrip("/")
API_TOKEN = os.environ.get("LIFECOACH_API_TOKEN", "").strip()
try:
    TIMEOUT = float(os.environ.get("LIFECOACH_TIMEOUT", "30"))
except ValueError:
    TIMEOUT = 30.0

SERVER_NAME = "lifecoach"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    """Stderr-only logger. Stdout is reserved for JSON-RPC frames."""
    sys.stderr.write(f"[lifecoach-mcp] {msg}\n")
    sys.stderr.flush()


# ──────────────────────────────────────────────────────────────────────────────
# Activity log — appends one JSONL line per tool call so the /ui/activity
# dashboard can render this MCP's calls. Best-effort; any failure swallowed.
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone  # noqa: E402
import json as _json  # noqa: E402

_ACTIVITY_DIR = os.environ.get("HERMES_HOME", "/data/.hermes") + "/activity"


def _activity_write(name: str, outcome: str, *, latency_ms: int | None = None,
                    summary: str | None = None, error: str | None = None) -> None:
    try:
        os.makedirs(_ACTIVITY_DIR, exist_ok=True)
        now = datetime.now(timezone.utc)
        year, week, _ = now.isocalendar()
        path = f"{_ACTIVITY_DIR}/activity-{year}-W{week:02d}.jsonl"
        rec: dict = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "tool_call",
            "source": "mcp_lifecoach",
            "name": name,
            "outcome": outcome,
        }
        if latency_ms is not None:
            rec["latency_ms"] = latency_ms
        if summary:
            rec["summary"] = summary[:200]
        if error:
            rec["error"] = error[:200]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# HTTP client — single shared client across the process lifetime.
# ──────────────────────────────────────────────────────────────────────────────

_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=BASE_URL,
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "User-Agent": f"lifecoach-mcp/{SERVER_VERSION}",
            },
        )
    return _client


def api_get(path: str, params: dict | None = None) -> dict:
    """GET a Life Coach API path, return parsed JSON. Raises on HTTP errors;
    callers turn that into MCP error responses."""
    client = get_client()
    # Strip None values so httpx doesn't serialize them as 'None'.
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    resp = client.get(path, params=clean)
    resp.raise_for_status()
    return resp.json()



def api_post(path: str, body: dict | None = None, method: str = "POST") -> dict:
    """POST/PUT/PATCH JSON to the API. A 409 on daily-goal creation is
    returned as data (it carries the existing goal) rather than raised."""
    client = get_client()
    clean = {k: v for k, v in (body or {}).items() if v is not None}
    resp = client.request(method, path, json=clean)
    if resp.status_code == 409:
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:300]}
        data.setdefault("conflict", True)
        return data
    resp.raise_for_status()
    return resp.json()


_DATE = {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
         "description": "YYYY-MM-DD (UTC calendar day)"}

_DATE_NOTE = (" Dates are UTC calendar days; the server's 'today' flips at "
              "8pm US-Eastern, so pass explicit dates when the user's local "
              "day matters.")

# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions — one per API endpoint (ping/manifest omitted).
# ──────────────────────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lifecoach_fitness_overview",
        "description": (
            "Today and this week at a glance: today's sleep score, 8-week "
            "sleep average, this week's cardio zone 3-4 minutes, today's "
            "steps, weekly and 28-day step averages. Call this first for any "
            "general 'how am I doing' fitness question." + _DATE_NOTE
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lifecoach_sleep_scores",
        "description": (
            "Nightly WHOOP sleep score per day over a date range, plus the "
            "range average. Default range is the last 21 days." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"from": _DATE, "to": _DATE},
        },
    },
    {
        "name": "lifecoach_sleep_summary",
        "description": "The 8-week average sleep score the app's Fitness page shows (Mon-Sun weeks).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lifecoach_cardio_minutes",
        "description": (
            "Minutes in heart-rate zones 3-4 per Mon-Sun week. WHOOP stores "
            "zones 3 and 4 combined, so only total_minutes is populated. "
            "Default range: last 8 weeks." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"from": _DATE, "to": _DATE},
        },
    },
    {
        "name": "lifecoach_steps",
        "description": (
            "Daily step counts (Fitbit) with daily and weekly averages. "
            "Default range: last 28 days." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"from": _DATE, "to": _DATE},
        },
    },
    {
        "name": "lifecoach_list_goals",
        "description": (
            "List goals. kind='daily' is the single 'commit to one thing no "
            "matter what' goal for a day; kind='general' is the multi-day "
            "to-do list. status defaults to open." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "done", "all"]},
                "kind": {"type": "string", "enum": ["daily", "general", "all"]},
                "date": {**_DATE, "description": "Daily goals for that day; general goals due or created that day."},
            },
        },
    },
    {
        "name": "lifecoach_add_goal",
        "description": (
            "Create a goal. kind='daily' makes the one daily goal for a date "
            "(default today UTC); if one already exists the response has "
            "conflict=true and the existing goal — tell the user, do not "
            "retry. kind='general' adds a to-do." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "required": ["kind", "title"],
            "properties": {
                "kind": {"type": "string", "enum": ["daily", "general"]},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "date": {**_DATE, "description": "Daily goals only. Default: today (UTC)."},
                "due_date": {**_DATE, "description": "General goals only."},
            },
        },
    },
    {
        "name": "lifecoach_complete_goal",
        "description": (
            "Mark a goal done. Idempotent. Use lifecoach_list_goals to find "
            "the id first. Note: a completed general goal stays visible in "
            "the app's list until the user checks it off there."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    },
    {
        "name": "lifecoach_uncomplete_goal",
        "description": "Reopen a goal that was marked done. Idempotent. Nothing is deleted.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    },
    {
        "name": "lifecoach_edit_goal",
        "description": "Edit a goal's title, notes, or due_date. Only supplied fields change.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due_date": _DATE,
            },
        },
    },
    {
        "name": "lifecoach_gratitude_list",
        "description": "Gratitude journal entries over a date range (default last 30 days), one per day." + _DATE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {"from": _DATE, "to": _DATE},
        },
    },
    {
        "name": "lifecoach_gratitude_today",
        "description": "Today's (UTC) gratitude entry, or entry=null if none yet." + _DATE_NOTE,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "lifecoach_save_gratitude",
        "description": (
            "Save what the user is grateful for on a given day. One entry per "
            "day; saving again replaces that day's text (upsert). Use this "
            "when the user says things like 'I'm grateful for ...'. Default "
            "date is today (UTC) — pass the user's local date when it is "
            "already evening in the US." + _DATE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Short phrase of gratitude."},
                "date": _DATE,
            },
        },
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Tool dispatch.
# ──────────────────────────────────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _req_str(a: dict, key: str) -> str:
    v = a.get(key)
    if not v or not isinstance(v, str):
        raise ValueError(f"{key} (string) is required")
    return v


def call_tool(name: str, arguments: dict) -> dict:
    """Execute one tool. Returns the JSON the LLM will see. Raises ValueError
    on bad input so the caller can surface a structured tool error."""
    a = arguments or {}
    rng = {"from": a.get("from"), "to": a.get("to")}

    if name == "lifecoach_fitness_overview":
        return api_get("/api/agent/fitness/overview")
    if name == "lifecoach_sleep_scores":
        return api_get("/api/agent/fitness/sleep", params=rng)
    if name == "lifecoach_sleep_summary":
        return api_get("/api/agent/fitness/sleep/summary")
    if name == "lifecoach_cardio_minutes":
        return api_get("/api/agent/fitness/cardio", params=rng)
    if name == "lifecoach_steps":
        return api_get("/api/agent/fitness/steps", params=rng)

    if name == "lifecoach_list_goals":
        data = api_get("/api/agent/goals", params={
            "status": a.get("status"), "kind": a.get("kind"), "date": a.get("date"),
        })
        return data if isinstance(data, dict) else {"goals": data}
    if name == "lifecoach_add_goal":
        kind = a.get("kind")
        if kind not in ("daily", "general"):
            raise ValueError("kind must be 'daily' or 'general'")
        body = {"kind": kind, "title": _req_str(a, "title"), "notes": a.get("notes")}
        if kind == "daily":
            body["date"] = a.get("date")
        else:
            body["due_date"] = a.get("due_date")
        return api_post("/api/agent/goals", body)
    if name == "lifecoach_complete_goal":
        return api_post(f"/api/agent/goals/{_req_str(a, 'id')}/complete")
    if name == "lifecoach_uncomplete_goal":
        return api_post(f"/api/agent/goals/{_req_str(a, 'id')}/uncomplete")
    if name == "lifecoach_edit_goal":
        gid = _req_str(a, "id")
        body = {k: a.get(k) for k in ("title", "notes", "due_date")}
        if not any(v is not None for v in body.values()):
            raise ValueError("supply at least one of title, notes, due_date")
        return api_post(f"/api/agent/goals/{gid}", body, method="PATCH")

    if name == "lifecoach_gratitude_list":
        data = api_get("/api/agent/gratitude", params=rng)
        return data if isinstance(data, dict) else {"entries": data}
    if name == "lifecoach_gratitude_today":
        return api_get("/api/agent/gratitude/today")
    if name == "lifecoach_save_gratitude":
        date = a.get("date") or _today_utc()
        return api_post(f"/api/agent/gratitude/{date}", {"text": _req_str(a, "text")}, method="PUT")

    raise ValueError(f"Unknown tool: {name}")


# ──────────────────────────────────────────────────────────────────────────────
# JSON-RPC framing over stdio.
# ──────────────────────────────────────────────────────────────────────────────

def send(msg: dict) -> None:
    """Emit one JSON-RPC frame on stdout. Newline-delimited, UTF-8, flushed."""
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_result(req_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    send({"jsonrpc": "2.0", "id": req_id, "error": err})


def handle_initialize(req_id: Any, _params: dict) -> None:
    reply_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_list(req_id: Any, _params: dict) -> None:
    reply_result(req_id, {"tools": TOOLS})


def handle_tools_call(req_id: Any, params: dict) -> None:
    import time as _time
    name = params.get("name", "")
    # Strip the `lifecoach_` prefix from the activity-log name so the
    # dashboard's tool-name column reads as `search`, not `lifecoach_search`.
    log_name = name[len("lifecoach_"):] if name.startswith("lifecoach_") else name
    arguments = params.get("arguments") or {}
    t0 = _time.time()
    try:
        result = call_tool(name, arguments)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        reply_result(req_id, {"content": [{"type": "text", "text": text}]})
        _activity_write(log_name, "ok",
                        latency_ms=int((_time.time() - t0) * 1000))
    except ValueError as e:
        # Caller-side error (bad input). Return as a tool-error result so the
        # LLM sees the message; not a JSON-RPC error envelope.
        reply_result(
            req_id,
            {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            },
        )
        _activity_write(log_name, "error",
                        latency_ms=int((_time.time() - t0) * 1000),
                        error=str(e))
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        msg = f"Life Coach API returned {e.response.status_code}: {body}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
        _activity_write(log_name, "error",
                        latency_ms=int((_time.time() - t0) * 1000),
                        error=f"HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        msg = f"Life Coach API request failed: {e}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
        _activity_write(log_name, "error",
                        latency_ms=int((_time.time() - t0) * 1000),
                        error=str(e)[:100])
    except Exception as e:  # pragma: no cover — defensive
        msg = f"Unexpected error in {name}: {e}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
        _activity_write(log_name, "error",
                        latency_ms=int((_time.time() - t0) * 1000),
                        error=str(e)[:100])


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def main() -> int:
    if not BASE_URL:
        log("LIFECOACH_BASE_URL not set — exiting cleanly so hermes "
            "marks the server unavailable rather than crashing the agent.")
        return 0
    if not API_TOKEN:
        log("LIFECOACH_API_TOKEN not set — exiting cleanly.")
        return 0

    log(f"booted; base_url={BASE_URL} token_len={len(API_TOKEN)} tools={len(TOOLS)}")

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad JSON on stdin: {e}")
            continue

        req_id = msg.get("id")
        method = msg.get("method", "")

        # Notifications have no `id` and require no response. Just ignore.
        if req_id is None and method.startswith("notifications/"):
            continue

        handler = HANDLERS.get(method)
        if handler is None:
            if req_id is not None:
                reply_error(req_id, -32601, f"Method not found: {method}")
            continue

        try:
            handler(req_id, msg.get("params") or {})
        except Exception as e:  # pragma: no cover — defensive
            log(f"handler {method} raised {type(e).__name__}: {e}")
            if req_id is not None:
                reply_error(req_id, -32603, f"Internal error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
