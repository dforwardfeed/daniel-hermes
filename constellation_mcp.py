#!/usr/bin/env python3
"""Constellation MCP server — exposes the user's read-only Constellation API
(YouTube-insight library) to Hermes as a stdio MCP server.

Runs as a subprocess spawned by hermes-agent. Communicates via line-delimited
JSON-RPC 2.0 over stdin/stdout. Logs to stderr. No external dependencies
beyond httpx (already in requirements.txt).

Env vars consumed (forwarded by server.py:_build_constellation_mcp_entry):
  CONSTELLATION_BASE_URL   required — e.g. https://your-app.replit.app
  CONSTELLATION_API_TOKEN  required — the AGENT_API_TOKEN secret
  CONSTELLATION_TIMEOUT    optional — request timeout in seconds (default 30)

Architecture mirrors the gbrain MCP entry: stdio transport, registered via
write_config_yaml under mcp_servers.constellation, env-forwarded from
os.environ at boot. Tools auto-namespace as `mcp_constellation_<tool>` on
the hermes side.

The API is documented at <BASE_URL>/api/agent/manifest. Tool definitions
below mirror endpoints 1:1; we don't expose /ping, /manifest, or /_audit
as tools because they're debug-only.
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

BASE_URL = os.environ.get("CONSTELLATION_BASE_URL", "").strip().rstrip("/")
API_TOKEN = os.environ.get("CONSTELLATION_API_TOKEN", "").strip()
try:
    TIMEOUT = float(os.environ.get("CONSTELLATION_TIMEOUT", "30"))
except ValueError:
    TIMEOUT = 30.0

SERVER_NAME = "constellation"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    """Stderr-only logger. Stdout is reserved for JSON-RPC frames."""
    sys.stderr.write(f"[constellation-mcp] {msg}\n")
    sys.stderr.flush()


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
                "User-Agent": f"constellation-mcp/{SERVER_VERSION}",
            },
        )
    return _client


def constellation_get(path: str, params: dict | None = None) -> dict:
    """GET a Constellation API path, return parsed JSON. Raises on HTTP errors;
    callers turn that into MCP error responses."""
    client = get_client()
    # Strip None values so httpx doesn't serialize them as 'None'.
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    resp = client.get(path, params=clean)
    resp.raise_for_status()
    return resp.json()


# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions. Each tool corresponds to one (or two-via-module) endpoint.
# Schemas are JSON Schema draft-07 compatible — what the MCP spec expects.
# ──────────────────────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "constellation_categories",
        "description": (
            "List the user's category taxonomy from both Brain modules "
            "(Original Brain = life/business; AI Brain = AI research). "
            "Returns predefined categories, custom user-created categories "
            "(with numeric ids for filtering), and per-module tag lists. "
            "Call this FIRST in any conversation where the user names a "
            "category in plain language, so you can resolve fuzzy names "
            "(e.g. \"GTM stuff\" → category id `sales`) before filtering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "constellation_semantic_search",
        "description": (
            "Vector-cosine search over the user's saved YouTube-insight "
            "library using pgvector embeddings. Best tool for conceptual / "
            "topical / \"what is the user interested in re: X\" questions. "
            "Returns up to `k` ranked results with a `distance` field "
            "(lower = closer; treat > 0.85 as weak). Falls back to keyword "
            "search semantics if the server lacks an OpenAI API key (503). "
            "Use `module` to scope to one Brain (original|ai) or `all`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Natural-language query (e.g. 'agent memory architectures', 'founder-led sales early on').",
                },
                "k": {
                    "type": "integer",
                    "description": "Max number of results (default 10, server max 100).",
                    "minimum": 1,
                    "maximum": 100,
                },
                "module": {
                    "type": "string",
                    "enum": ["all", "original", "ai"],
                    "description": "Which Brain to search. Default `all`.",
                },
            },
            "required": ["q"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constellation_search",
        "description": (
            "Fast keyword (ILIKE substring) search across saved insights. "
            "Use when the user wants an exact phrase or token (e.g. 'find "
            "every block that mentions PMF'). Returns hits split by module. "
            "Prefer constellation_semantic_search for conceptual questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Exact keyword or short phrase to match.",
                },
                "module": {
                    "type": "string",
                    "enum": ["all", "original", "ai"],
                    "description": "Which Brain to search. Default `all`.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per module (default 50).",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": ["q"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constellation_library",
        "description": (
            "List saved insights from ONE Brain module, optionally filtered "
            "by category, custom category id, source video, or keyword. Use "
            "this when the user names a specific category (\"show me my "
            "Health insights\"). For Original Brain, predefined category "
            "ids include: ai, health, personal-development, sales, product, "
            "finance, racing, venture-capital, founder-tips, biography. "
            "For AI Brain: ai-agents, ai-mcp-cli-tools, ai-memory, ai-voice, "
            "ai-reinforcement-learning, ai-continual-learning, ai-sandbox, "
            "ai-physical, ai-coding, ai-science, ai-gpu-compute. Custom "
            "categories are addressed by numeric `customCategoryId` instead "
            "of the string `category` — discover them via "
            "constellation_categories."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "enum": ["original", "ai"],
                    "description": "Which Brain to read from. Required.",
                },
                "category": {
                    "type": "string",
                    "description": "Predefined category id (e.g. `sales`, `ai-agents`). Mutually exclusive with customCategoryId.",
                },
                "customCategoryId": {
                    "type": "integer",
                    "description": "Numeric id of a user-created custom category. Mutually exclusive with `category`.",
                },
                "videoId": {
                    "type": "integer",
                    "description": "Only return insights extracted from a specific source video id.",
                },
                "q": {
                    "type": "string",
                    "description": "Optional keyword filter (ILIKE on content + video title).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50, max 500).",
                    "minimum": 1,
                    "maximum": 500,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0).",
                    "minimum": 0,
                },
            },
            "required": ["module"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constellation_library_all",
        "description": (
            "List saved insights from BOTH Brain modules, merged and "
            "recency-sorted. Each returned item carries its `module` field. "
            "Use when the user wants a cross-Brain overview without naming "
            "a category."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Optional keyword filter (ILIKE on content + video title).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100).",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "constellation_get_video",
        "description": (
            "Fetch the full record for ONE source video: youtubeUrl, title, "
            "transcript, app-generated summary, and the category it was "
            "filed under. Use this AFTER finding an interesting insight via "
            "search/library to dig into the underlying source. The video id "
            "comes from any LibraryItem's `videoId` field."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Numeric video id (from a LibraryItem's videoId field).",
                },
                "module": {
                    "type": "string",
                    "enum": ["original", "ai"],
                    "description": "Which Brain module the video lives in. Required (the two modules have separate video tables).",
                },
            },
            "required": ["id", "module"],
            "additionalProperties": False,
        },
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Tool dispatch.
# ──────────────────────────────────────────────────────────────────────────────

def call_tool(name: str, arguments: dict) -> dict:
    """Run a tool by name. Returns the JSON-serializable result the MCP
    client will see (wrapped in content[].text by the caller). Raises on
    invalid inputs so the caller can surface a structured error."""
    a = arguments or {}

    if name == "constellation_categories":
        return constellation_get("/api/agent/categories")

    if name == "constellation_semantic_search":
        q = a.get("q")
        if not q or not isinstance(q, str):
            raise ValueError("q (string) is required")
        return constellation_get(
            "/api/agent/semantic-search",
            params={"q": q, "k": a.get("k"), "module": a.get("module")},
        )

    if name == "constellation_search":
        q = a.get("q")
        if not q or not isinstance(q, str):
            raise ValueError("q (string) is required")
        return constellation_get(
            "/api/agent/search",
            params={"q": q, "module": a.get("module"), "limit": a.get("limit")},
        )

    if name == "constellation_library":
        module = a.get("module")
        if module not in ("original", "ai"):
            raise ValueError("module must be 'original' or 'ai'")
        path = "/api/agent/library" if module == "original" else "/api/agent/ai-library"
        return constellation_get(
            path,
            params={
                "category": a.get("category"),
                "customCategoryId": a.get("customCategoryId"),
                "videoId": a.get("videoId"),
                "q": a.get("q"),
                "limit": a.get("limit"),
                "offset": a.get("offset"),
            },
        )

    if name == "constellation_library_all":
        return constellation_get(
            "/api/agent/library/all",
            params={"q": a.get("q"), "limit": a.get("limit")},
        )

    if name == "constellation_get_video":
        video_id = a.get("id")
        module = a.get("module")
        if not isinstance(video_id, int):
            raise ValueError("id (integer) is required")
        if module not in ("original", "ai"):
            raise ValueError("module must be 'original' or 'ai'")
        path = f"/api/agent/videos/{video_id}" if module == "original" else f"/api/agent/ai-videos/{video_id}"
        return constellation_get(path)

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
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    try:
        result = call_tool(name, arguments)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        reply_result(req_id, {"content": [{"type": "text", "text": text}]})
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
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        msg = f"Constellation API returned {e.response.status_code}: {body}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
    except httpx.HTTPError as e:
        msg = f"Constellation API request failed: {e}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
    except Exception as e:  # pragma: no cover — defensive
        msg = f"Unexpected error in {name}: {e}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def main() -> int:
    if not BASE_URL:
        log("CONSTELLATION_BASE_URL not set — exiting cleanly so hermes "
            "marks the server unavailable rather than crashing the agent.")
        return 0
    if not API_TOKEN:
        log("CONSTELLATION_API_TOKEN not set — exiting cleanly.")
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
