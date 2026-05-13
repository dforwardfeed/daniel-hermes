#!/usr/bin/env python3
"""GenUI MCP server — exposes the user-defined views API to Hermes so the
agent can create / manage views and their items via typed tools.

Runs as a subprocess of hermes-agent, registered under `mcp_servers.genui`
in /data/.hermes/config.yaml. Communicates via line-delimited JSON-RPC 2.0
over stdin/stdout. Logs to stderr. Sibling design of constellation_mcp.py:
same framing, same auth pattern (bearer the GENUI_API_TOKEN against the
same-container HTTP server).

Env vars consumed (forwarded by server.py:_build_genui_mcp_entry):
  GENUI_API_TOKEN   required — bearer token for /api/ui/views/* writes
  GENUI_BASE_URL    optional — defaults to http://127.0.0.1:8642 (same container)
  GENUI_TIMEOUT     optional — request timeout in seconds (default 15)

Six tools (auto-namespaced by hermes as `mcp_genui_<tool>`):

  genui_list_views        return slug/name/description/itemCount for every view
  genui_create_view       create a new named view (default kind=checklist)
  genui_delete_view       permanently delete a view and all its items
  genui_add_item          append an item to a view; returns the new id
  genui_mark_done         toggle an item's `done` flag (done=true|false)
  genui_remove_item       remove a single item from a view

The agent should call `genui_list_views` first when the user names a view
in plain language so it can resolve "my todo list" → slug `todo`. New
views should be created with deliberate, durable slugs (kebab-case,
12 chars or less is ideal).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


# Force UTF-8 on stdio. Linux default is UTF-8, Windows defaults to cp1252
# which raises UnicodeEncodeError on non-ASCII in tool descriptions.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("GENUI_BASE_URL", "").strip().rstrip("/")
if not BASE_URL:
    # Sensible default: same container, same Starlette server. server.py
    # binds to $PORT (Railway sets it; default to the value we use).
    port = os.environ.get("PORT", "8642").strip() or "8642"
    BASE_URL = f"http://127.0.0.1:{port}"
API_TOKEN = os.environ.get("GENUI_API_TOKEN", "").strip()
try:
    TIMEOUT = float(os.environ.get("GENUI_TIMEOUT", "15"))
except ValueError:
    TIMEOUT = 15.0

SERVER_NAME = "genui"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def log(msg: str) -> None:
    """Stderr-only logger; stdout is reserved for JSON-RPC frames."""
    sys.stderr.write(f"[genui-mcp] {msg}\n")
    sys.stderr.flush()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP client — single shared httpx.Client across process lifetime.
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
                "X-Genui-Token": API_TOKEN,
                "User-Agent": f"genui-mcp/{SERVER_VERSION}",
                "Content-Type": "application/json",
            },
        )
    return _client


def api_call(method: str, path: str, body: dict | None = None) -> dict:
    """Call the GenUI HTTP API. Raises httpx.HTTPStatusError on non-2xx so
    the caller can surface a structured tool error."""
    client = get_client()
    resp = client.request(method, path, json=body if body is not None else None)
    resp.raise_for_status()
    # Some endpoints return 201 with a body; some return 200 with {ok: true}.
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return {"ok": True, "status": resp.status_code}


# ──────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ──────────────────────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "genui_list_views",
        "description": (
            "List every user-defined view in the GenUI portal. Each entry "
            "carries the view's `slug`, `name`, `description`, `kind` "
            "(currently always 'checklist'), and `itemCount`. Call this "
            "FIRST whenever the user names a view in plain language ('my "
            "todo list', 'the reading list') so you can resolve the human "
            "name to its persistent `slug`. The slug is what every other "
            "tool below expects as input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "genui_create_view",
        "description": (
            "Create a new user-defined view (a persistent named section in "
            "the GenUI portal that appears alongside Library and Daily). "
            "Use this when the user says 'create a view called X' or "
            "'make me a Y list'. The view appears at /ui/view/<slug> and "
            "in the shared topbar nav. Each view stores its own items; "
            "items can be added/marked-done/removed via the other tools "
            "below. Slugs must be lowercase-kebab-case and unique — if "
            "you don't pass one, the server slugifies the name for you "
            "(e.g. name='Reading List' → slug='reading-list'). Returns "
            "the resolved slug and the canonical view URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name shown in the topbar and page header (e.g. 'To-Do List', 'Groceries').",
                },
                "slug": {
                    "type": "string",
                    "description": "Optional kebab-case slug. Auto-derived from name when omitted. Must match /^[a-z][a-z0-9-]+$/ and not collide with reserved words (latest, saved, daily, views, view, etc.).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional one-line description rendered under the view's title (<= 500 chars).",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional initial items to seed the view with. Each item: {text: string, done?: boolean, note?: string}. Useful when creating a view from a list the user just dictated.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "genui_delete_view",
        "description": (
            "Permanently delete a view and ALL of its items. Use only "
            "when the user explicitly asks to remove a view. There is no "
            "undo — confirm in the chat before calling this if the view "
            "carries items. Affects only the named view; other views are "
            "untouched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The kebab-case slug of the view to delete.",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
    {
        "name": "genui_add_item",
        "description": (
            "Append a new item to an existing view. Use this when the "
            "user says 'add X to my todo list' or 'put Y on the reading "
            "list'. The item starts as not-done; the server assigns its "
            "id and creation timestamp. Returns the new item including "
            "its assigned `id` so subsequent mark-done / remove calls "
            "can reference it directly without re-listing the view."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The view's slug. Resolve via genui_list_views if you only know the human name.",
                },
                "text": {
                    "type": "string",
                    "description": "The item's text content (<= 2000 chars). Required.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional sub-note shown below the item text (<= 2000 chars).",
                },
            },
            "required": ["slug", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "genui_mark_done",
        "description": (
            "Toggle an item's `done` flag. Pass `done: true` when the "
            "user says they finished a task; `done: false` to re-open a "
            "previously-completed item. Done items remain in the view "
            "(shown strikethrough under a 'Done' section) — they are "
            "not deleted. To actually remove an item, call "
            "`genui_remove_item` instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The view's slug.",
                },
                "item_id": {
                    "type": "string",
                    "description": "The item's id (`i_…`). From genui_add_item's response or from a prior genui_list_views fetch.",
                },
                "done": {
                    "type": "boolean",
                    "description": "true = mark complete; false = re-open. Required.",
                },
            },
            "required": ["slug", "item_id", "done"],
            "additionalProperties": False,
        },
    },
    {
        "name": "genui_remove_item",
        "description": (
            "Permanently remove a single item from a view. Use only when "
            "the user explicitly asks to delete an item (vs. mark it "
            "done). No undo. Other items in the view are unaffected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The view's slug.",
                },
                "item_id": {
                    "type": "string",
                    "description": "The item's id (`i_…`).",
                },
            },
            "required": ["slug", "item_id"],
            "additionalProperties": False,
        },
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Tool dispatch
# ──────────────────────────────────────────────────────────────────────────────

def call_tool(name: str, arguments: dict) -> dict:
    a = arguments or {}

    if name == "genui_list_views":
        # Endpoint returns the trimmed-summary shape suited for an LLM.
        return api_call("GET", "/api/ui/views")

    if name == "genui_create_view":
        view_name = a.get("name")
        if not isinstance(view_name, str) or not view_name.strip():
            raise ValueError("name (string) is required")
        body: dict[str, Any] = {"name": view_name, "kind": "checklist"}
        if isinstance(a.get("slug"), str) and a["slug"].strip():
            body["slug"] = a["slug"].strip()
        if isinstance(a.get("description"), str):
            body["description"] = a["description"]
        if isinstance(a.get("items"), list):
            body["items"] = a["items"]
        return api_call("POST", "/api/ui/views", body)

    if name == "genui_delete_view":
        slug = a.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug (string) is required")
        return api_call("DELETE", f"/api/ui/views/{slug}")

    if name == "genui_add_item":
        slug = a.get("slug")
        text = a.get("text")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug (string) is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text (string) is required")
        body = {"text": text}
        if isinstance(a.get("note"), str):
            body["note"] = a["note"]
        return api_call("POST", f"/api/ui/views/{slug}/items", body)

    if name == "genui_mark_done":
        slug = a.get("slug")
        item_id = a.get("item_id")
        done = a.get("done")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug (string) is required")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id (string) is required")
        if not isinstance(done, bool):
            raise ValueError("done (boolean) is required")
        return api_call("PATCH", f"/api/ui/views/{slug}/items/{item_id}", {"done": done})

    if name == "genui_remove_item":
        slug = a.get("slug")
        item_id = a.get("item_id")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("slug (string) is required")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id (string) is required")
        return api_call("DELETE", f"/api/ui/views/{slug}/items/{item_id}")

    raise ValueError(f"Unknown tool: {name}")


# ──────────────────────────────────────────────────────────────────────────────
# JSON-RPC framing (line-delimited UTF-8 over stdio)
# ──────────────────────────────────────────────────────────────────────────────

def send(msg: dict) -> None:
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
        msg = f"GenUI API returned {e.response.status_code}: {body}"
        log(msg)
        reply_result(
            req_id,
            {"content": [{"type": "text", "text": msg}], "isError": True},
        )
    except httpx.HTTPError as e:
        msg = f"GenUI API request failed: {e}"
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
    if not API_TOKEN:
        log("GENUI_API_TOKEN not set — exiting cleanly so hermes marks the "
            "server unavailable rather than failing every tool call.")
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
