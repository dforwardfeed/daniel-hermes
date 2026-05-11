"""GenUI artifact portal — store and render structured UI artifacts.

Artifacts are JSON blobs that describe a renderable UI: title, category,
viewType, source, payload, and a renderSpec that tells the server how to
render it. For the MVP only `renderSpec.kind == "template"` is fully wired:
the server picks a Jinja template by name and feeds it `payload` + `props`.

`json-render` and `openui` are validated as kinds but render to a placeholder
page — they're hooks for a future React/JSON-driven renderer.

Storage layout (under GENUI_STORAGE, default /data/genui):

    artifacts/<id>.json    one file per artifact; the `status` field
                           distinguishes "temporary" vs "saved"

Auth model:
    UI pages (/ui/*) and API routes (/api/ui/*) reuse the same cookie guard
    as the setup wizard, passed in by server.py via get_routes(). For
    server-to-server posting (e.g. GBrain in the same container), set
    GENUI_API_TOKEN — requests carrying `Authorization: Bearer <token>` or
    `X-Genui-Token: <token>` bypass the cookie check.
"""

from __future__ import annotations

import hmac as _hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates


# Wired up in get_routes(); module-level so handlers can reach them.
_templates: Jinja2Templates | None = None
_guard: Callable[[Request], Response | None] | None = None


# ── Config (env-var driven) ───────────────────────────────────────────────────
def _envbool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("false", "0", "no", "off", "")


GENUI_ENABLED = _envbool("GENUI_ENABLED", True)
GENUI_STORAGE = Path(os.environ.get("GENUI_STORAGE", "/data/genui"))
GENUI_BASE_URL = os.environ.get("GENUI_BASE_URL", "").rstrip("/")
try:
    GENUI_TEMPORARY_TTL_HOURS = max(1, int(os.environ.get("GENUI_TEMPORARY_TTL_HOURS", "72")))
except ValueError:
    GENUI_TEMPORARY_TTL_HOURS = 72
GENUI_AUTO_SAVE_CATEGORIES = {
    s.strip()
    for s in os.environ.get("GENUI_AUTO_SAVE_CATEGORIES", "daily_briefing,portfolio,jobs").split(",")
    if s.strip()
}
GENUI_API_TOKEN = os.environ.get("GENUI_API_TOKEN", "")

ARTIFACTS_DIR = GENUI_STORAGE / "artifacts"


# ── Validation tables ─────────────────────────────────────────────────────────
VALID_CATEGORIES = {
    "finance", "briefing", "search", "graph", "timeline", "jobs",
    "stats", "reports", "custom", "daily_briefing", "portfolio",
}
VALID_VIEW_TYPES = {
    "dashboard", "table", "graph", "timeline", "document", "status", "custom",
    # Chart-shaped views — accepted as cosmetic/semantic labels. The actual
    # rendering choice is driven by renderSpec.template, not viewType, so
    # being permissive here doesn't change rendering behavior; it just
    # avoids 400ing legitimate chart artifacts that callers (e.g. GBrain's
    # render_chart middleware) emit with these names.
    "chart", "line_chart", "bar_chart", "pie_chart", "area_chart", "scatter_chart",
    # Card / markdown views — emitted by GBrain's UI_RULES for find_orphans,
    # get_backlinks, and any LLM view-picker route that lands on
    # generic_cards (TEMPLATE_CATALOG view: 'cards'). The viewType is
    # cosmetic — actual rendering is still driven by renderSpec.template
    # (e.g. generic_cards). Hermes-side docs already claim these are
    # accepted (see gbrain/docs/genui-portal-templates.md); this catches
    # the validator up with the documented contract.
    "cards", "markdown",
}
VALID_RENDER_KINDS = {"template", "json-render", "openui"}
VALID_TRANSPORTS = {"stdio", "http", "unknown"}
VALID_TRIGGERS = {"chat", "cron", "job", "manual"}
SUPPORTED_TEMPLATES = {
    "search_table",
    "stats_dashboard",
    "timeline_view",
    "jobs_status",
    "generic_cards",
    "line_chart",
    # Path A additions — round out the template catalog so the LLM
    # view-picker has more options than just table / cards / line_chart:
    "bar_chart",          # vertical bars, same payload shape as line_chart
    "markdown_doc",       # markdown → HTML; unlocks unstructured-prose UI
    "comparison_table",   # two-column side-by-side compare
    "metric_callout",     # single hero stat for "what's my X?" answers
}
DAILY_CATEGORIES = {"daily_briefing", "briefing", "stats", "reports"}

# `ui_` followed by URL-safe chars (no path traversal possible).
ID_RE = re.compile(r"^ui_[A-Za-z0-9]{8,32}$")


# ── Storage helpers ───────────────────────────────────────────────────────────
def _ensure_dirs() -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    # token_urlsafe gives us [A-Za-z0-9_-]; we strip _- to satisfy ID_RE
    # and pad if needed. 16 alnum chars = ~95 bits of entropy, plenty.
    raw = secrets.token_urlsafe(16).replace("-", "").replace("_", "")
    return f"ui_{raw[:16].ljust(16, 'a')}"


def _is_expired(art: dict) -> bool:
    if art.get("status") == "saved":
        return False
    exp = art.get("expiresAt")
    if not exp or not isinstance(exp, str):
        return False
    try:
        d = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) > d


def _path_for(art_id: str) -> Path | None:
    if not isinstance(art_id, str) or not ID_RE.match(art_id):
        return None
    return ARTIFACTS_DIR / f"{art_id}.json"


def _load(art_id: str) -> dict | None:
    p = _path_for(art_id)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_artifact(art: dict) -> None:
    _ensure_dirs()
    p = _path_for(art["id"])
    if p is None:
        raise ValueError("invalid artifact id")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _list_artifacts(
    *,
    status_filter: str | None = None,
    category_filter: str | None = None,
    limit: int = 200,
) -> list[dict]:
    _ensure_dirs()
    files = sorted(
        ARTIFACTS_DIR.glob("ui_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out: list[dict] = []
    for f in files:
        try:
            art = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _is_expired(art):
            # Lazy GC — drop expired temporaries on read.
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if status_filter and art.get("status") != status_filter:
            continue
        if category_filter and art.get("category") != category_filter:
            continue
        out.append(art)
        if len(out) >= limit:
            break
    return out


# ── Per-template payload validators ───────────────────────────────────────────
# Generic shape is enforced by _validate_create; these run on top for templates
# whose renderer needs a more specific structure to produce sensible output.

def _validate_line_chart_payload(payload: dict) -> list[str]:
    """line_chart needs at least one series with at least one numeric point.
    Each point must carry an `x` (any scalar; rendered as a label) and a `y`
    (number, or string parseable as number). Returns a list of error strings;
    empty == valid."""
    errs: list[str] = []
    series = payload.get("series")
    if not isinstance(series, list) or not series:
        errs.append("payload.series must be a non-empty array")
        return errs
    for i, s in enumerate(series):
        if not isinstance(s, dict):
            errs.append(f"payload.series[{i}] must be an object")
            continue
        if not s.get("name"):
            errs.append(f"payload.series[{i}].name required (string)")
        points = s.get("points")
        if not isinstance(points, list) or not points:
            errs.append(f"payload.series[{i}].points must be a non-empty array")
            continue
        for j, p in enumerate(points):
            if not isinstance(p, dict):
                errs.append(f"payload.series[{i}].points[{j}] must be an object")
                continue
            if "x" not in p:
                errs.append(f"payload.series[{i}].points[{j}].x required")
            if "y" not in p:
                errs.append(f"payload.series[{i}].points[{j}].y required")
            else:
                try:
                    float(p["y"])
                except (TypeError, ValueError):
                    errs.append(
                        f"payload.series[{i}].points[{j}].y must be numeric "
                        f"(got type {type(p['y']).__name__})"
                    )
    return errs


# ── Path A: validators for the new templates ──────────────────────────────────
# bar_chart shares the line_chart payload contract (series of {name, points})
# because the data shape is the same — the difference is purely the renderer's
# choice of bars-vs-lines. Aliased so future divergence stays cheap.
_validate_bar_chart_payload = _validate_line_chart_payload


def _validate_markdown_doc_payload(payload: dict) -> list[str]:
    """markdown_doc requires a non-empty markdown string. Optional summary,
    optional list of source citations."""
    errs: list[str] = []
    md = payload.get("markdown")
    if not isinstance(md, str) or not md.strip():
        errs.append("payload.markdown required (non-empty string)")
    elif len(md) > 500_000:
        # Hard cap to avoid blowing up the markdown renderer. ~500KB is well
        # above any reasonable LLM output and still serves quickly.
        errs.append(f"payload.markdown too long ({len(md)} chars, max 500000)")
    sources = payload.get("sources")
    if sources is not None and not isinstance(sources, list):
        errs.append("payload.sources must be a list if provided")
    return errs


def _validate_comparison_table_payload(payload: dict) -> list[str]:
    """comparison_table needs left/right column headers and at least one row.
    Each row must declare a label plus the left/right values being compared.
    Optional `highlight` per row marks the winner (`left` | `right` | `tie`)."""
    errs: list[str] = []
    for side in ("left", "right"):
        col = payload.get(side)
        if not isinstance(col, dict):
            errs.append(f"payload.{side} must be an object")
            continue
        if not col.get("label"):
            errs.append(f"payload.{side}.label required (string)")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        errs.append("payload.rows must be a non-empty array")
        return errs
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errs.append(f"payload.rows[{i}] must be an object")
            continue
        if not row.get("label"):
            errs.append(f"payload.rows[{i}].label required (string)")
        if "left" not in row:
            errs.append(f"payload.rows[{i}].left required")
        if "right" not in row:
            errs.append(f"payload.rows[{i}].right required")
        hl = row.get("highlight")
        if hl is not None and hl not in ("left", "right", "tie"):
            errs.append(
                f"payload.rows[{i}].highlight={hl!r} must be one of "
                f"'left', 'right', 'tie' (or omitted)"
            )
    return errs


def _validate_metric_callout_payload(payload: dict) -> list[str]:
    """metric_callout shows one giant number/value with optional context.
    Required: value. Optional: label, delta, delta_kind, context, sources."""
    errs: list[str] = []
    if "value" not in payload:
        errs.append("payload.value required")
    delta_kind = payload.get("delta_kind")
    if delta_kind is not None and delta_kind not in ("up", "down", "neutral"):
        errs.append(
            f"payload.delta_kind={delta_kind!r} must be one of "
            f"'up', 'down', 'neutral' (or omitted)"
        )
    sources = payload.get("sources")
    if sources is not None and not isinstance(sources, list):
        errs.append("payload.sources must be a list if provided")
    return errs


# Map template name → optional payload validator. Adding a new entry here
# wires per-template validation without touching _validate_create.
_TEMPLATE_PAYLOAD_VALIDATORS: dict[str, "Callable[[dict], list[str]]"] = {
    "line_chart":        _validate_line_chart_payload,
    "bar_chart":         _validate_bar_chart_payload,
    "markdown_doc":      _validate_markdown_doc_payload,
    "comparison_table":  _validate_comparison_table_payload,
    "metric_callout":    _validate_metric_callout_payload,
}


# ── Phase C: server-side json-render kind ─────────────────────────────────────
# Generative UI inspired by Vercel Labs' @json-render/core library, but
# rendered server-side in Python so the deploy stays HTML-only (no React
# bundle, no JS build pipeline). Spec format is wire-compatible with the
# json-render React renderer — a future Hermes upgrade can serve the same
# artifacts to a client-side bundle without changing the gbrain emitter.
#
# Wire format (lifted from json-render docs):
#   {
#     "root": "<element_id>",
#     "elements": {
#       "<element_id>": {
#         "type": "<ComponentName>",   // must be in JSON_RENDER_CATALOG
#         "props": { ... },             // shape per component's schema
#         "children": ["<id>", ...]     // optional, list of child IDs
#       },
#       ...
#     }
#   }
#
# Safety design:
#   - Every string prop is html-escaped before emission.
#   - href / src values must use http, https, mailto, or tel — same allowlist
#     as the markdown_doc renderer. javascript: / data: rejected at validate.
#   - Hard caps on element count + recursion depth so a hostile spec can't
#     exhaust memory or stack.
#   - Cycle detection: an element can appear at most once in the rendered
#     output. Revisiting an id during traversal = rejected at validate.
#   - Unknown component types → validator rejects (NOT a placeholder render).
#     This is intentional: a typo'd component shouldn't silently disappear.

JSON_RENDER_MAX_ELEMENTS = 500
JSON_RENDER_MAX_DEPTH = 20
_JR_ALLOWED_PROTOCOLS = ("http://", "https://", "mailto:", "tel:")


def _jr_escape(value) -> str:
    """HTML-escape a prop value for inline emission. Coerces non-strings via
    str() then escapes — handles numbers, bools, None safely."""
    import html as _html
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _jr_check_url(href: str) -> bool:
    """Allow only the four protocols the markdown_doc renderer permits.
    Bare protocol-relative (//evil.com), data:, javascript:, file: all
    rejected."""
    if not isinstance(href, str) or not href:
        return False
    lower = href.lower().lstrip()
    # Relative paths starting with / are allowed (in-app links).
    if lower.startswith("/") and not lower.startswith("//"):
        return True
    return any(lower.startswith(p) for p in _JR_ALLOWED_PROTOCOLS)


# Component catalog. Each entry declares:
#   required_props: set of prop names that MUST be present (string-typed)
#   optional_props: set of prop names that MAY be present
#   url_props:      subset whose values must pass _jr_check_url
#   enum_props:     dict of prop name → tuple of allowed values
#   container:      True if the component renders its children (others ignore
#                   the children: [] field — declared so validators don't fail
#                   when a caller emits it anyway)
JSON_RENDER_CATALOG: dict[str, dict] = {
    # ── Layout
    "Container": {
        "optional_props": {"padding", "maxWidth", "background"},
        "container": True,
    },
    "Card": {
        "optional_props": {"title", "tag"},
        "container": True,
    },
    "Stack": {
        "optional_props": {"direction", "gap", "align"},
        "enum_props": {"direction": ("row", "column"), "align": ("start", "center", "end", "stretch")},
        "container": True,
    },
    "Grid": {
        "optional_props": {"columns", "gap", "minWidth"},
        "container": True,
    },
    "Divider": {"container": False},

    # ── Text
    "Heading": {
        "required_props": {"text"},
        "optional_props": {"level"},
        "enum_props": {"level": ("h1", "h2", "h3", "h4", "h5", "h6")},
        "container": False,
    },
    "Paragraph": {
        "required_props": {"text"},
        "optional_props": {"muted"},
        "container": False,
    },
    "Code": {
        "required_props": {"code"},
        "optional_props": {"lang"},
        "container": False,
    },
    "Quote": {
        "required_props": {"text"},
        "optional_props": {"source"},
        "container": False,
    },
    "Link": {
        "required_props": {"href", "text"},
        "optional_props": {"target"},
        "url_props": {"href"},
        "enum_props": {"target": ("_blank", "_self")},
        "container": False,
    },

    # ── Data
    "Metric": {
        "required_props": {"label", "value"},
        "optional_props": {"delta", "deltaKind", "format"},
        "enum_props": {
            "deltaKind": ("up", "down", "neutral"),
            "format": ("number", "currency", "percent", "string"),
        },
        "container": False,
    },
    "KeyValueList": {
        "required_props": {"items"},  # items: list of {key, value}
        "container": False,
    },
    "Tag": {
        "required_props": {"text"},
        "container": False,
    },
    "Badge": {
        "required_props": {"text"},
        "optional_props": {"kind"},
        "enum_props": {"kind": ("success", "warning", "error", "info", "neutral")},
        "container": False,
    },

    # ── Media
    "Image": {
        "required_props": {"src", "alt"},
        "optional_props": {"width", "height"},
        "url_props": {"src"},
        "container": False,
    },
}


def _validate_json_render_payload(payload: dict) -> list[str]:
    """Validate a json-render spec. Returns a list of human-readable error
    strings; empty list means valid. Errors mention the offending element id
    + prop name so the LLM can self-correct on retry."""
    errs: list[str] = []
    if not isinstance(payload, dict):
        errs.append("payload must be an object with `root` and `elements`")
        return errs

    root_id = payload.get("root")
    elements = payload.get("elements")

    if not isinstance(root_id, str) or not root_id:
        errs.append("payload.root required (non-empty string)")
    if not isinstance(elements, dict) or not elements:
        errs.append("payload.elements required (non-empty object keyed by id)")
        return errs

    if len(elements) > JSON_RENDER_MAX_ELEMENTS:
        errs.append(
            f"payload.elements has {len(elements)} entries — exceeds the "
            f"{JSON_RENDER_MAX_ELEMENTS} cap. Simplify the spec."
        )
        return errs

    if root_id not in elements:
        errs.append(f"payload.root={root_id!r} not present in payload.elements")
        # Continue so we still flag bad element shapes below.

    # Validate every element's shape + component-specific schema.
    for elem_id, elem in elements.items():
        if not isinstance(elem_id, str) or not elem_id:
            errs.append(f"elements key must be a non-empty string (saw {elem_id!r})")
            continue
        if not isinstance(elem, dict):
            errs.append(f"elements[{elem_id!r}] must be an object")
            continue
        comp = elem.get("type")
        if comp not in JSON_RENDER_CATALOG:
            errs.append(
                f"elements[{elem_id!r}].type={comp!r} not in catalog. "
                f"Allowed: {sorted(JSON_RENDER_CATALOG)}"
            )
            continue

        schema = JSON_RENDER_CATALOG[comp]
        props = elem.get("props") or {}
        if not isinstance(props, dict):
            errs.append(f"elements[{elem_id!r}].props must be an object")
            continue

        # Required props present?
        for rp in schema.get("required_props", set()):
            if rp not in props:
                errs.append(f"elements[{elem_id!r}].props.{rp} required for type={comp}")

        # Enum props within allowed values?
        for ep, allowed in schema.get("enum_props", {}).items():
            if ep in props and props[ep] not in allowed:
                errs.append(
                    f"elements[{elem_id!r}].props.{ep}={props[ep]!r} must be one of {list(allowed)}"
                )

        # URL props pass protocol allowlist?
        for up in schema.get("url_props", set()):
            if up in props and not _jr_check_url(props[up]):
                errs.append(
                    f"elements[{elem_id!r}].props.{up}={props[up]!r} must use http://, "
                    f"https://, mailto:, tel:, or be a relative path starting with /"
                )

        # KeyValueList.items shape — easier to validate inline than as a generic recursive schema.
        if comp == "KeyValueList":
            items = props.get("items")
            if not isinstance(items, list):
                errs.append(f"elements[{elem_id!r}].props.items must be a list")
            else:
                for j, it in enumerate(items):
                    if not isinstance(it, dict) or "key" not in it or "value" not in it:
                        errs.append(
                            f"elements[{elem_id!r}].props.items[{j}] must be "
                            "an object with `key` and `value`"
                        )

        # Children references all resolve?
        children = elem.get("children")
        if children is not None:
            if not isinstance(children, list):
                errs.append(f"elements[{elem_id!r}].children must be a list or omitted")
            else:
                for cidx, child_id in enumerate(children):
                    if not isinstance(child_id, str):
                        errs.append(
                            f"elements[{elem_id!r}].children[{cidx}] must be a string id"
                        )
                    elif child_id not in elements:
                        errs.append(
                            f"elements[{elem_id!r}].children[{cidx}]={child_id!r} "
                            "does not exist in elements"
                        )

    # Reachability + cycle / depth detection — only run if no structural errors
    # above (otherwise the traversal would produce noise on already-broken specs).
    if not errs and root_id in elements:
        seen: set[str] = set()
        def walk(node_id: str, depth: int) -> None:
            if depth > JSON_RENDER_MAX_DEPTH:
                errs.append(
                    f"render depth exceeded {JSON_RENDER_MAX_DEPTH} levels "
                    f"(at {node_id!r}); reduce nesting"
                )
                return
            if node_id in seen:
                errs.append(
                    f"cycle or duplicate reference at element {node_id!r} — "
                    "each element may be rendered at most once"
                )
                return
            seen.add(node_id)
            elem = elements.get(node_id) or {}
            comp = elem.get("type")
            schema = JSON_RENDER_CATALOG.get(comp, {})
            if schema.get("container"):
                for cid in elem.get("children") or []:
                    if isinstance(cid, str) and cid in elements:
                        walk(cid, depth + 1)
        walk(root_id, 0)

    return errs


# ── json-render renderers ─────────────────────────────────────────────────────
# One render function per component. Each takes a props dict (already
# validated upstream) and a pre-rendered children HTML string, returns the
# HTML for this element. CSS classes piggy-back on _base.html's design tokens
# (`card`, `tag`, `kpi`, `status-pill`, etc.) so json-render output looks at
# home next to template-rendered artifacts.

def _jr_render_Container(props: dict, children: str) -> str:
    style_parts = []
    if "padding" in props:
        # Accept either a number (treated as px) or a string CSS length.
        v = props["padding"]
        if isinstance(v, (int, float)):
            style_parts.append(f"padding:{int(v)}px")
        elif isinstance(v, str):
            style_parts.append(f"padding:{_jr_escape(v)}")
    if "maxWidth" in props:
        v = props["maxWidth"]
        if isinstance(v, (int, float)):
            style_parts.append(f"max-width:{int(v)}px")
        elif isinstance(v, str):
            style_parts.append(f"max-width:{_jr_escape(v)}")
    if "background" in props and isinstance(props["background"], str):
        style_parts.append(f"background:{_jr_escape(props['background'])}")
    style = f' style="{";".join(style_parts)}"' if style_parts else ""
    return f'<div class="jr-container"{style}>{children}</div>'


def _jr_render_Card(props: dict, children: str) -> str:
    title = props.get("title")
    tag = props.get("tag")
    parts = ['<div class="card">']
    if tag:
        parts.append(f'<span class="tag">{_jr_escape(tag)}</span>')
    if title:
        parts.append(f'<h3 style="font-size:15px;font-weight:500;color:#f0f6fc;margin:6px 0 8px">{_jr_escape(title)}</h3>')
    parts.append(children)
    parts.append('</div>')
    return "".join(parts)


def _jr_render_Stack(props: dict, children: str) -> str:
    direction = props.get("direction", "column")
    gap = props.get("gap", 12)
    align = props.get("align", "stretch")
    gap_px = int(gap) if isinstance(gap, (int, float)) else 12
    return (
        f'<div style="display:flex;flex-direction:{_jr_escape(direction)};'
        f'gap:{gap_px}px;align-items:{_jr_escape(align)}">{children}</div>'
    )


def _jr_render_Grid(props: dict, children: str) -> str:
    columns = props.get("columns")
    gap = props.get("gap", 14)
    min_width = props.get("minWidth", 220)
    gap_px = int(gap) if isinstance(gap, (int, float)) else 14
    min_px = int(min_width) if isinstance(min_width, (int, float)) else 220
    if isinstance(columns, int) and columns > 0:
        tmpl = f"repeat({columns}, 1fr)"
    else:
        tmpl = f"repeat(auto-fit, minmax({min_px}px, 1fr))"
    return f'<div style="display:grid;grid-template-columns:{tmpl};gap:{gap_px}px">{children}</div>'


def _jr_render_Divider(_props: dict, _children: str) -> str:
    return '<hr style="border:none;border-top:1px solid #252d3d;margin:18px 0">'


def _jr_render_Heading(props: dict, _children: str) -> str:
    level = props.get("level", "h2")
    return f"<{level} style=\"color:#f0f6fc;font-weight:600;margin:14px 0 8px\">{_jr_escape(props.get('text'))}</{level}>"


def _jr_render_Paragraph(props: dict, _children: str) -> str:
    cls = "muted" if props.get("muted") else ""
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<p{cls_attr} style="font-size:14px;margin:8px 0">{_jr_escape(props.get("text"))}</p>'


def _jr_render_Code(props: dict, _children: str) -> str:
    lang = props.get("lang", "")
    lang_attr = f' data-lang="{_jr_escape(lang)}"' if lang else ""
    return f'<pre{lang_attr}><code>{_jr_escape(props.get("code"))}</code></pre>'


def _jr_render_Quote(props: dict, _children: str) -> str:
    src = props.get("source")
    src_html = (
        f'<footer style="margin-top:6px;font-size:11px;color:#6b7688">— {_jr_escape(src)}</footer>'
        if src else ""
    )
    return (
        '<blockquote style="border-left:3px solid #6272ff;padding:6px 14px;margin:12px 0;'
        f'color:#9aa6b8;font-style:italic;background:rgba(98,114,255,0.04);border-radius:0 6px 6px 0">'
        f"<p>{_jr_escape(props.get('text'))}</p>{src_html}</blockquote>"
    )


def _jr_render_Link(props: dict, _children: str) -> str:
    target = props.get("target", "_self")
    rel = ' rel="noopener"' if target == "_blank" else ""
    return (
        f'<a href="{_jr_escape(props["href"])}" target="{_jr_escape(target)}"{rel}>'
        f'{_jr_escape(props.get("text"))}</a>'
    )


def _jr_format_metric(value, fmt: str) -> str:
    """Format a Metric value per the format prop. Mirrors line_chart's
    _format_y but with a wider input type set (Metric accepts strings)."""
    if fmt in ("currency", "percent", "number") and isinstance(value, (int, float)):
        if fmt == "currency":
            return f"${value:,.2f}" if abs(value) >= 1 else f"${value:.2f}"
        if fmt == "percent":
            return f"{value:.1f}%"
        if isinstance(value, int):
            return f"{value:,}"
        return f"{value:,.2f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _jr_render_Metric(props: dict, _children: str) -> str:
    fmt = props.get("format", "number")
    val = _jr_format_metric(props.get("value"), fmt)
    delta = props.get("delta")
    delta_kind = props.get("deltaKind", "neutral")
    delta_html = ""
    if delta:
        cls = "up" if delta_kind == "up" else ("down" if delta_kind == "down" else "")
        delta_html = f'<div class="kpi-delta {cls}">{_jr_escape(delta)}</div>'
    return (
        '<div>'
        f'<div class="kpi-label">{_jr_escape(props.get("label"))}</div>'
        f'<div class="kpi">{_jr_escape(val)}</div>'
        f'{delta_html}'
        '</div>'
    )


def _jr_render_KeyValueList(props: dict, _children: str) -> str:
    items = props.get("items") or []
    rows = []
    for it in items:
        rows.append(
            '<dt class="muted" style="font-family:\'IBM Plex Mono\',monospace;'
            'font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-top:8px">'
            f"{_jr_escape(it.get('key'))}</dt>"
            f'<dd style="color:#c9d1d9;font-size:13px">{_jr_escape(it.get("value"))}</dd>'
        )
    return f'<dl style="margin:0">{"".join(rows)}</dl>'


def _jr_render_Tag(props: dict, _children: str) -> str:
    return f'<span class="tag">{_jr_escape(props.get("text"))}</span>'


def _jr_render_Badge(props: dict, _children: str) -> str:
    kind = props.get("kind", "neutral")
    # Map our 5 kinds to existing CSS classes when possible, else inline color.
    if kind == "success":
        return f'<span class="status-pill saved">{_jr_escape(props.get("text"))}</span>'
    if kind == "warning":
        return f'<span class="status-pill temporary">{_jr_escape(props.get("text"))}</span>'
    if kind == "error":
        return (
            '<span class="status-pill" style="background:rgba(248,81,73,0.15);color:#f85149">'
            f'{_jr_escape(props.get("text"))}</span>'
        )
    if kind == "info":
        return (
            '<span class="status-pill" style="background:rgba(98,114,255,0.15);color:#7b8fff">'
            f'{_jr_escape(props.get("text"))}</span>'
        )
    return f'<span class="status-pill">{_jr_escape(props.get("text"))}</span>'


def _jr_render_Image(props: dict, _children: str) -> str:
    w = props.get("width")
    h = props.get("height")
    w_attr = f' width="{int(w)}"' if isinstance(w, (int, float)) else ""
    h_attr = f' height="{int(h)}"' if isinstance(h, (int, float)) else ""
    return (
        f'<img src="{_jr_escape(props["src"])}" alt="{_jr_escape(props.get("alt"))}"'
        f'{w_attr}{h_attr} style="max-width:100%;height:auto;border-radius:6px">'
    )


_JR_RENDERERS = {
    "Container":    _jr_render_Container,
    "Card":         _jr_render_Card,
    "Stack":        _jr_render_Stack,
    "Grid":         _jr_render_Grid,
    "Divider":      _jr_render_Divider,
    "Heading":      _jr_render_Heading,
    "Paragraph":    _jr_render_Paragraph,
    "Code":         _jr_render_Code,
    "Quote":        _jr_render_Quote,
    "Link":         _jr_render_Link,
    "Metric":       _jr_render_Metric,
    "KeyValueList": _jr_render_KeyValueList,
    "Tag":          _jr_render_Tag,
    "Badge":        _jr_render_Badge,
    "Image":        _jr_render_Image,
}


def _render_json_render_spec(payload: dict) -> str | None:
    """Render a validated json-render spec to an HTML string. Returns None
    if the spec is unrenderable for any reason (validator should have caught
    it, but be defensive — stale saved artifacts can drift past the
    validator). Recursive but capped by JSON_RENDER_MAX_DEPTH at validate
    time so we know we won't blow the stack."""
    root_id = payload.get("root")
    elements = payload.get("elements") or {}
    if not isinstance(root_id, str) or not isinstance(elements, dict):
        return None
    if root_id not in elements:
        return None

    rendered: dict[str, str] = {}

    def render_node(node_id: str, depth: int) -> str:
        if depth > JSON_RENDER_MAX_DEPTH:
            return ""
        if node_id in rendered:
            return rendered[node_id]
        elem = elements.get(node_id)
        if not isinstance(elem, dict):
            return ""
        comp = elem.get("type")
        renderer = _JR_RENDERERS.get(comp)
        if renderer is None:
            return ""
        props = elem.get("props") or {}
        schema = JSON_RENDER_CATALOG.get(comp, {})
        # Recurse children for container components only.
        children_html = ""
        if schema.get("container"):
            child_ids = elem.get("children") or []
            children_html = "".join(
                render_node(cid, depth + 1) for cid in child_ids if isinstance(cid, str)
            )
        out = renderer(props, children_html)
        rendered[node_id] = out
        return out

    try:
        return render_node(root_id, 0)
    except Exception as e:
        print(f"[genui][json-render] render failed for root={root_id!r}: {e!r}", flush=True)
        return None


def _prepare_json_render_ctx(payload: dict) -> dict | None:
    html = _render_json_render_spec(payload)
    if html is None:
        return None
    return {
        "html": html,
        "element_count": len(payload.get("elements") or {}),
    }


# ── Validation ────────────────────────────────────────────────────────────────
def _validate_create(body: dict) -> tuple[dict, list[str]]:
    """Return (artifact, errors). artifact is {} when errors is non-empty."""
    if not isinstance(body, dict):
        return ({}, ["request body must be a JSON object"])

    errs: list[str] = []

    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        errs.append("title required (non-empty string)")
    elif len(title) > 300:
        errs.append("title too long (max 300 chars)")

    category = body.get("category", "custom")
    if category not in VALID_CATEGORIES:
        errs.append(
            f"category={category!r} must be one of {sorted(VALID_CATEGORIES)}"
        )

    view_type = body.get("viewType", "custom")
    if view_type not in VALID_VIEW_TYPES:
        errs.append(
            f"viewType={view_type!r} must be one of {sorted(VALID_VIEW_TYPES)}"
        )

    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        errs.append("payload must be an object")

    rs = body.get("renderSpec") or {}
    if not isinstance(rs, dict):
        errs.append("renderSpec must be an object")
        rs = {}
    kind = rs.get("kind")
    if kind not in VALID_RENDER_KINDS:
        errs.append(f"renderSpec.kind must be one of {sorted(VALID_RENDER_KINDS)}")
    if kind == "template":
        tpl = rs.get("template")
        if tpl not in SUPPORTED_TEMPLATES:
            errs.append(
                f"renderSpec.template must be one of {sorted(SUPPORTED_TEMPLATES)} for kind=template"
            )
        elif isinstance(payload, dict):
            # Per-template payload-shape validation (only runs when generic
            # checks have passed enough that we have a valid template name
            # and a dict payload to inspect).
            extra = _TEMPLATE_PAYLOAD_VALIDATORS.get(tpl)
            if extra is not None:
                errs.extend(extra(payload))
    elif kind == "json-render":
        # Phase C — server-side generative UI. Payload IS the spec
        # ({root, elements}). Validate against the catalog so a malformed
        # spec gets a useful error message instead of a placeholder render.
        if isinstance(payload, dict):
            errs.extend(_validate_json_render_payload(payload))
    props = rs.get("props", {})
    if props is not None and not isinstance(props, dict):
        errs.append("renderSpec.props must be an object if provided")

    source = body.get("source") or {}
    if not isinstance(source, dict):
        errs.append("source must be an object")
        source = {}
    transport = source.get("transport", "unknown")
    if transport not in VALID_TRANSPORTS:
        errs.append(f"source.transport must be one of {sorted(VALID_TRANSPORTS)}")
    trigger = source.get("trigger", "manual")
    if trigger not in VALID_TRIGGERS:
        errs.append(f"source.trigger must be one of {sorted(VALID_TRIGGERS)}")
    params_summary = source.get("paramsSummary", {})
    if not isinstance(params_summary, dict):
        params_summary = {}

    if errs:
        return ({}, errs)

    now = _now_iso()
    auto_save = category in GENUI_AUTO_SAVE_CATEGORIES
    status = "saved" if auto_save else "temporary"

    art = {
        "id": _new_id(),
        "title": title.strip(),
        "category": category,
        "viewType": view_type,
        "status": status,
        "source": {
            "operation": str(source.get("operation", ""))[:300],
            "paramsSummary": params_summary,
            "transport": transport,
            "trigger": trigger,
        },
        "payload": payload,
        "renderSpec": {
            "kind": kind,
            "template": rs.get("template", "") if kind == "template" else "",
            "props": props if isinstance(props, dict) else {},
        },
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": None if status == "saved" else _expires_iso(GENUI_TEMPORARY_TTL_HOURS),
    }
    return (art, [])


# ── URL construction ──────────────────────────────────────────────────────────
def _base_url(request: Request) -> str:
    if GENUI_BASE_URL:
        return GENUI_BASE_URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


# ── Auth ──────────────────────────────────────────────────────────────────────
def _has_api_token(request: Request) -> bool:
    if not GENUI_API_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
        if provided and _hmac.compare_digest(provided, GENUI_API_TOKEN):
            return True
    x_token = request.headers.get("x-genui-token", "")
    if x_token and _hmac.compare_digest(x_token, GENUI_API_TOKEN):
        return True
    return False


def _api_guard(request: Request) -> Response | None:
    if _has_api_token(request):
        return None
    return _guard(request) if _guard else JSONResponse({"error": "Unauthorized"}, status_code=401)


def _ui_guard(request: Request) -> Response | None:
    return _guard(request) if _guard else None


def _disabled() -> Response:
    return JSONResponse({"error": "GenUI disabled"}, status_code=503)


# ── API handlers ──────────────────────────────────────────────────────────────
async def api_create(request: Request) -> Response:
    if not GENUI_ENABLED:
        return _disabled()
    if err := _api_guard(request):
        return err
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    art, errs = _validate_create(body)
    if errs:
        # Log validation failures to stdout so we can debug GBrain (or any
        # caller) without needing access to the response body. We log:
        #   - top-level keys we received (so we know if `payload` / `renderSpec`
        #     /`source` are missing entirely vs malformed)
        #   - the renderSpec.template if present (so we can tell which template
        #     they were aiming at)
        #   - the first 5 error messages verbatim — error strings are field
        #     names + constraints, never values, so secrets can't leak.
        body_keys = sorted(body.keys()) if isinstance(body, dict) else []
        rs = body.get("renderSpec") if isinstance(body, dict) else None
        attempted_template = (
            rs.get("template") if isinstance(rs, dict) else None
        ) or (rs.get("kind") if isinstance(rs, dict) else None)
        client_host = request.client.host if request.client else "unknown"
        print(
            f"[genui] rejected POST (400) client={client_host} "
            f"template={attempted_template!r} body_keys={body_keys} "
            f"errs={errs[:5]}",
            flush=True,
        )
        return JSONResponse({"error": "validation failed", "details": errs}, status_code=400)

    try:
        _save_artifact(art)
    except OSError:
        # Don't leak filesystem details to the caller.
        return JSONResponse({"error": "failed to persist artifact"}, status_code=500)

    # Audit log — lets us see in deploy logs whether GBrain (or any caller)
    # is actually POSTing artifacts. No payload contents, no token, just
    # what + by whom.
    auth_via = "bearer" if _has_api_token(request) else "cookie"
    print(
        f"[genui] created id={art['id']} template={art['renderSpec'].get('template') or art['renderSpec'].get('kind')} "
        f"category={art['category']} status={art['status']} auth={auth_via} "
        f"client={request.client.host if request.client else 'unknown'}",
        flush=True,
    )

    base = _base_url(request)
    return JSONResponse(
        {
            "id": art["id"],
            "url": f"{base}/ui/latest/{art['id']}",
            "status": art["status"],
        },
        status_code=201,
    )


async def api_get(request: Request) -> Response:
    if not GENUI_ENABLED:
        return _disabled()
    if err := _api_guard(request):
        return err
    art = _load(request.path_params["id"])
    if not art or _is_expired(art):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(art)


async def api_list(request: Request) -> Response:
    if not GENUI_ENABLED:
        return _disabled()
    if err := _api_guard(request):
        return err
    status_filter = request.query_params.get("status") or None
    category_filter = request.query_params.get("category") or None
    items = _list_artifacts(status_filter=status_filter, category_filter=category_filter)
    summaries = [
        {
            "id": a["id"],
            "title": a.get("title", ""),
            "category": a.get("category", ""),
            "viewType": a.get("viewType", ""),
            "status": a.get("status", ""),
            "createdAt": a.get("createdAt", ""),
            "updatedAt": a.get("updatedAt", ""),
            "expiresAt": a.get("expiresAt"),
            "source": {"operation": a.get("source", {}).get("operation", "")},
        }
        for a in items
    ]
    return JSONResponse({"artifacts": summaries, "count": len(summaries)})


async def api_save(request: Request) -> Response:
    if not GENUI_ENABLED:
        return _disabled()
    if err := _api_guard(request):
        return err
    art = _load(request.path_params["id"])
    if not art or _is_expired(art):
        return JSONResponse({"error": "Not found"}, status_code=404)
    art["status"] = "saved"
    art["expiresAt"] = None
    art["updatedAt"] = _now_iso()
    try:
        _save_artifact(art)
    except OSError:
        return JSONResponse({"error": "failed to update artifact"}, status_code=500)
    return JSONResponse({"ok": True, "id": art["id"], "status": "saved"})


async def api_delete(request: Request) -> Response:
    if not GENUI_ENABLED:
        return _disabled()
    if err := _api_guard(request):
        return err
    p = _path_for(request.path_params["id"])
    if p is None or not p.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        p.unlink()
    except OSError:
        return JSONResponse({"error": "delete failed"}, status_code=500)
    return JSONResponse({"ok": True})


# ── line_chart SVG coordinate pre-compute ─────────────────────────────────────
# Doing this in Python (rather than via Jinja math) keeps the template flat:
# it just iterates pre-rendered series/x_labels/y_ticks. Pure function — no
# I/O, no shared state — so it's trivial to unit-test.

_LINE_CHART_PALETTE = ["#6272ff", "#3fb950", "#d29922", "#f85149", "#7b8fff", "#ff7eb6"]
# SVG geometry: kept in one place so styling tweaks don't ripple through the template.
_LC_W, _LC_H = 880, 380
_LC_PL, _LC_PR, _LC_PT, _LC_PB = 90, 30, 30, 60  # padding: left/right/top/bottom


def _format_y(value: float, fmt: str) -> str:
    """Format a y-axis value per payload.y_format. Falls back to a sensible
    numeric repr (no thousand separators — keeps the SVG narrow)."""
    if fmt == "currency":
        return f"${value:,.2f}" if abs(value) >= 1 else f"${value:.2f}"
    if fmt == "percent":
        return f"{value:.1f}%"
    if fmt == "integer":
        return f"{int(round(value)):,}"
    # Default: drop trailing zeros for clean ticks.
    return f"{value:,.2f}".rstrip("0").rstrip(".") or "0"


def _axis_label(value) -> str:
    """Coerce an axis spec to a display label.

    Accepts either:
      - A plain string label (`"Year"`) — what our README example uses.
      - A dict with a `.label` field (`{"label":"Year","field":"x"}`) —
        what GBrain's render_chart middleware emits.

    Returns "" for anything else so the SVG just omits the axis title.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        lbl = value.get("label")
        if isinstance(lbl, str):
            return lbl
    return ""


def _y_format(payload: dict) -> str:
    """Resolve the y-format hint from either `payload.y_format` (our README
    shape) or `payload.y_axis.format` (GBrain's middleware shape)."""
    fmt = payload.get("y_format")
    if isinstance(fmt, str) and fmt:
        return fmt
    yaxis = payload.get("y_axis")
    if isinstance(yaxis, dict):
        nested = yaxis.get("format")
        if isinstance(nested, str):
            return nested
    return ""


def _prepare_line_chart_ctx(payload: dict) -> dict | None:
    """Pre-compute everything line_chart.html needs to render an SVG line
    chart from a validated payload. Returns None if the payload is so broken
    that rendering would fail (caller falls back to error.html)."""
    series_in = payload.get("series", [])
    if not isinstance(series_in, list) or not series_in:
        return None

    # Coerce y to float; drop bad points silently (validator already ran upstream
    # — anything that gets here should be well-formed, but be defensive).
    cleaned: list[dict] = []
    all_y: list[float] = []
    for s in series_in:
        if not isinstance(s, dict):
            continue
        points = s.get("points")
        if not isinstance(points, list):
            continue
        cleaned_pts: list[dict] = []
        for p in points:
            if not isinstance(p, dict):
                continue
            try:
                y = float(p.get("y"))
            except (TypeError, ValueError):
                continue
            cleaned_pts.append({"x": p.get("x", ""), "y": y})
            all_y.append(y)
        if cleaned_pts:
            cleaned.append({"name": s.get("name", ""), "points": cleaned_pts})

    if not cleaned or not all_y:
        return None

    y_min, y_max = min(all_y), max(all_y)
    if y_min == y_max:
        # Flat data — pad so the line sits mid-chart, not flush against an axis.
        pad = abs(y_max) * 0.1 or 1.0
        y_min -= pad
        y_max += pad
    y_range = y_max - y_min

    plot_w = _LC_W - _LC_PL - _LC_PR
    plot_h = _LC_H - _LC_PT - _LC_PB

    fmt = _y_format(payload)

    rendered_series = []
    for idx, s in enumerate(cleaned):
        n = len(s["points"])
        coords = []
        for i, p in enumerate(s["points"]):
            x_pos = _LC_PL + (i / (n - 1)) * plot_w if n > 1 else _LC_PL + plot_w / 2
            y_pos = _LC_PT + plot_h - ((p["y"] - y_min) / y_range) * plot_h
            coords.append({
                "x_pos": round(x_pos, 2),
                "y_pos": round(y_pos, 2),
                "x_label": str(p["x"]),
                "y_value": p["y"],
                "y_label": _format_y(p["y"], fmt),
            })
        rendered_series.append({
            "name": s["name"],
            "color": _LINE_CHART_PALETTE[idx % len(_LINE_CHART_PALETTE)],
            "coords": coords,
            "polyline": " ".join(f"{c['x_pos']},{c['y_pos']}" for c in coords),
        })

    # X-axis labels: take from the longest series so we cover the full timeline.
    canonical = max(rendered_series, key=lambda s: len(s["coords"]))
    x_labels = [
        {"x_pos": c["x_pos"], "x_label": c["x_label"]}
        for c in canonical["coords"]
    ]

    # Y-axis: 5 evenly-spaced ticks.
    y_ticks = []
    for i in range(5):
        frac = i / 4
        val = y_min + frac * y_range
        y_pos = _LC_PT + plot_h - frac * plot_h
        y_ticks.append({
            "y_pos": round(y_pos, 2),
            "y_label": _format_y(val, fmt),
        })

    return {
        "W": _LC_W,
        "H": _LC_H,
        "PL": _LC_PL,
        "PR": _LC_PR,
        "PT": _LC_PT,
        "PB": _LC_PB,
        "plot_w": plot_w,
        "plot_h": plot_h,
        "y_min_label": _format_y(y_min, fmt),
        "y_max_label": _format_y(y_max, fmt),
        "series": rendered_series,
        "x_labels": x_labels,
        "y_ticks": y_ticks,
        "x_axis_label": _axis_label(payload.get("x_axis")),
        "y_axis_label": _axis_label(payload.get("y_axis")),
        "title": payload.get("title", ""),
        "source_slug": payload.get("source_slug", ""),
    }


# ── bar_chart SVG coordinate pre-compute ──────────────────────────────────────
# Shares the line_chart payload contract (series → points) but renders as
# vertical bars grouped by x-label. Pure function; trivial to unit-test.

def _prepare_bar_chart_ctx(payload: dict) -> dict | None:
    """Pre-compute SVG bar geometry. Multi-series → grouped bars side-by-side
    per x-label. Single series → one bar per x-label. Y-axis baseline is 0
    when all values are non-negative; otherwise it crosses at 0 with negative
    bars drawn downward. Returns None on payload corruption."""
    series_in = payload.get("series", [])
    if not isinstance(series_in, list) or not series_in:
        return None

    cleaned: list[dict] = []
    all_y: list[float] = []
    # Preserve x-label order across series (assume first series defines canonical order).
    x_order: list = []
    seen_x: set = set()
    for s in series_in:
        if not isinstance(s, dict):
            continue
        points = s.get("points")
        if not isinstance(points, list):
            continue
        cleaned_pts: list[dict] = []
        for p in points:
            if not isinstance(p, dict):
                continue
            try:
                y = float(p.get("y"))
            except (TypeError, ValueError):
                continue
            x_val = p.get("x", "")
            cleaned_pts.append({"x": x_val, "y": y})
            all_y.append(y)
            key = str(x_val)
            if key not in seen_x:
                seen_x.add(key)
                x_order.append(x_val)
        if cleaned_pts:
            cleaned.append({"name": s.get("name", ""), "points": cleaned_pts})

    if not cleaned or not all_y:
        return None

    y_max = max(all_y)
    y_min = min(all_y)
    # Anchor baseline at 0 unless data is entirely negative; otherwise we'd
    # exaggerate small differences by floating the baseline.
    baseline = 0.0
    if y_min >= 0:
        baseline = 0.0
        y_lo = 0.0
        y_hi = y_max if y_max > 0 else 1.0
    elif y_max <= 0:
        baseline = 0.0
        y_lo = y_min
        y_hi = 0.0
    else:
        y_lo, y_hi = y_min, y_max
    y_range = y_hi - y_lo
    if y_range == 0:
        y_range = 1.0

    plot_w = _LC_W - _LC_PL - _LC_PR
    plot_h = _LC_H - _LC_PT - _LC_PB

    fmt = _y_format(payload)

    n_groups = len(x_order)
    n_series = len(cleaned)
    # Group width carves the plot horizontally; bars within a group share that slot.
    group_w = plot_w / n_groups if n_groups else plot_w
    # Leave 20% group padding so groups don't visually touch each other.
    bar_slot = group_w * 0.8
    bar_w = bar_slot / n_series if n_series else bar_slot
    group_start_offset = (group_w - bar_slot) / 2

    def y_to_pos(v: float) -> float:
        return _LC_PT + plot_h - ((v - y_lo) / y_range) * plot_h

    baseline_y = y_to_pos(baseline)

    rendered_series = []
    for s_idx, s in enumerate(cleaned):
        color = _LINE_CHART_PALETTE[s_idx % len(_LINE_CHART_PALETTE)]
        # Index points by x for quick lookup against canonical x_order.
        by_x = {str(p["x"]): p for p in s["points"]}
        bars = []
        for g_idx, x_val in enumerate(x_order):
            p = by_x.get(str(x_val))
            if p is None:
                continue
            y_pos = y_to_pos(p["y"])
            top = min(y_pos, baseline_y)
            height = abs(y_pos - baseline_y)
            x_pos = _LC_PL + g_idx * group_w + group_start_offset + s_idx * bar_w
            bars.append({
                "x_pos": round(x_pos, 2),
                "y_pos": round(top, 2),
                "width": round(bar_w * 0.92, 2),  # small gap between bars in a group
                "height": round(max(height, 1.0), 2),
                "x_label": str(x_val),
                "y_value": p["y"],
                "y_label": _format_y(p["y"], fmt),
            })
        rendered_series.append({"name": s["name"], "color": color, "bars": bars})

    # X-axis labels: one per group, centered.
    x_labels = [
        {
            "x_pos": _LC_PL + g_idx * group_w + group_w / 2,
            "x_label": str(x),
        }
        for g_idx, x in enumerate(x_order)
    ]

    # Y-axis: 5 evenly-spaced ticks.
    y_ticks = []
    for i in range(5):
        frac = i / 4
        val = y_lo + frac * y_range
        y_pos = _LC_PT + plot_h - frac * plot_h
        y_ticks.append({"y_pos": round(y_pos, 2), "y_label": _format_y(val, fmt)})

    return {
        "W": _LC_W,
        "H": _LC_H,
        "PL": _LC_PL,
        "PR": _LC_PR,
        "PT": _LC_PT,
        "PB": _LC_PB,
        "plot_w": plot_w,
        "plot_h": plot_h,
        "baseline_y": round(baseline_y, 2),
        "series": rendered_series,
        "x_labels": x_labels,
        "y_ticks": y_ticks,
        "x_axis_label": _axis_label(payload.get("x_axis")),
        "y_axis_label": _axis_label(payload.get("y_axis")),
        "title": payload.get("title", ""),
        "source_slug": payload.get("source_slug", ""),
        "multi_series": n_series > 1,
    }


# ── markdown_doc HTML pre-compute ─────────────────────────────────────────────
# Convert payload.markdown to HTML server-side. Two-stage pipeline:
#   1. python-markdown renders the markdown source to HTML. It does NOT
#      sanitize raw HTML inside the source; <script>...</script> would pass
#      through verbatim.
#   2. bleach runs the output through a strict tag + attribute allowlist
#      (defined as _MD_ALLOWED_TAGS / _MD_ALLOWED_ATTRS below). Everything
#      not in the list — script, iframe, on* handlers, javascript: URLs —
#      is stripped.
# Both stages must succeed before we emit HTML; if bleach is missing we
# return None and fall back to error.html rather than serving unsafe HTML.

# Tags the renderer is allowed to emit. Extending this is a security-
# sensitive change — only add tags whose props can't carry side effects
# (no <object>, <embed>, <form>, <input>, etc.).
_MD_ALLOWED_TAGS = frozenset({
    # Headings + paragraphs
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
    # Emphasis
    "strong", "em", "b", "i", "u", "del", "s", "sub", "sup", "mark",
    # Lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # Links + media
    "a", "img",
    # Blockquote + code
    "blockquote", "code", "pre", "kbd", "samp", "var",
    # Tables (markdown.extensions.tables emits these)
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    # Containers (markdown TOC + sane_lists wrap in these)
    "div", "span",
})
_MD_ALLOWED_ATTRS = {
    "a":   ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "width", "height"],
    "*":   ["id", "class"],  # for TOC anchors + heading IDs
    "th":  ["align"],
    "td":  ["align"],
}
# URL schemes allowed for <a href> and <img src>. javascript: + data: are
# excluded by omission. mailto:, tel:, and http(s): cover the realistic LLM
# output set without enabling code execution vectors.
_MD_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto", "tel"})


def _prepare_markdown_doc_ctx(payload: dict) -> dict | None:
    """Render the supplied markdown to safe HTML. Returns None when either
    `markdown` or `bleach` is missing (logged with a clear stderr line) —
    callers fall back to error.html rather than serving unsanitized output."""
    try:
        import markdown as _md
    except ImportError as e:
        print(f"[genui][markdown_doc] markdown library missing: {e!r}", flush=True)
        return None
    try:
        import bleach as _bleach
    except ImportError as e:
        # Critical — bleach is the sanitizer. Refuse to render rather than
        # emit unsafe HTML if it's somehow missing in the image.
        print(
            f"[genui][markdown_doc] bleach library missing: {e!r} — refusing "
            "to render unsanitized markdown. Add bleach to requirements.txt.",
            flush=True,
        )
        return None

    src = payload.get("markdown") or ""
    # Extensions we want:
    #   - tables (LLM often outputs pipe tables)
    #   - fenced_code (triple-backtick blocks)
    #   - sane_lists (cleaner ordered/unordered list edges)
    #   - toc (anchored headings for in-page nav; payload.toc=true to enable)
    extensions = ["tables", "fenced_code", "sane_lists"]
    if payload.get("toc") is True:
        extensions.append("toc")

    try:
        raw_html = _md.markdown(
            src,
            extensions=extensions,
            output_format="html5",
        )
    except Exception as e:
        print(f"[genui][markdown_doc] markdown render failed: {e!r}", flush=True)
        return None

    try:
        html = _bleach.clean(
            raw_html,
            tags=_MD_ALLOWED_TAGS,
            attributes=_MD_ALLOWED_ATTRS,
            protocols=_MD_ALLOWED_PROTOCOLS,
            strip=True,            # remove disallowed tags entirely (not just escape)
            strip_comments=True,   # comments can carry conditional-IE script tricks
        )
    except Exception as e:
        print(f"[genui][markdown_doc] bleach sanitize failed: {e!r}", flush=True)
        return None

    return {
        "html": html,
        "summary": payload.get("summary", ""),
        "sources": payload.get("sources") or [],
        "toc_enabled": payload.get("toc") is True,
    }


# ── comparison_table pre-compute ──────────────────────────────────────────────

def _prepare_comparison_table_ctx(payload: dict) -> dict | None:
    """Light normalization for comparison_table. The validator has already
    ensured shape; this just maps highlight values to CSS classes the
    template uses so the Jinja stays declarative."""
    left = payload.get("left") or {}
    right = payload.get("right") or {}
    rows_in = payload.get("rows") or []

    rows = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        hl = r.get("highlight")
        rows.append({
            "label": r.get("label", ""),
            "left": r.get("left", ""),
            "right": r.get("right", ""),
            "highlight": hl if hl in ("left", "right", "tie") else None,
            "note": r.get("note", ""),
        })

    return {
        "left_label":    left.get("label", "Left"),
        "left_sublabel": left.get("sublabel", ""),
        "right_label":   right.get("label", "Right"),
        "right_sublabel": right.get("sublabel", ""),
        "rows":          rows,
        "summary":       payload.get("summary", ""),
        "verdict":       payload.get("verdict", ""),
    }


# ── metric_callout pre-compute ────────────────────────────────────────────────

def _prepare_metric_callout_ctx(payload: dict) -> dict | None:
    """Format a single hero metric. delta_kind controls the up/down/neutral
    coloring without forcing the caller to know the CSS classes."""
    value = payload.get("value")
    if value is None:
        return None

    # Coerce numeric values to a tidy display string. Strings pass through
    # untouched so the caller can render "42 of 100" or "≈$1.2B" as-is.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Drop trailing zeros on floats; thousand-sep on integers above 999.
        if isinstance(value, float):
            display = f"{value:,.2f}".rstrip("0").rstrip(".") or "0"
        else:
            display = f"{value:,}"
    else:
        display = str(value)

    return {
        "value":       display,
        "label":       payload.get("label", ""),
        "delta":       payload.get("delta", ""),
        "delta_kind":  payload.get("delta_kind", "neutral"),
        "context":     payload.get("context", ""),
        "footnote":    payload.get("footnote", ""),
        "sources":     payload.get("sources") or [],
    }


# ── UI handlers ───────────────────────────────────────────────────────────────
def _template_for(art: dict) -> str:
    rs = art.get("renderSpec", {}) or {}
    kind = rs.get("kind")
    if kind == "template":
        name = rs.get("template", "")
        if name in SUPPORTED_TEMPLATES:
            return f"genui/{name}.html"
    if kind == "json-render":
        # Phase C — server-side generative UI. The actual element tree is
        # pre-rendered to an HTML string in _prepare_json_render_ctx and
        # injected as `jr.html` into a thin wrapper template.
        return "genui/json_render.html"
    # openui / unknown all fall through to a placeholder.
    return "genui/error.html"


def _render_artifact(request: Request, art: dict) -> Response:
    assert _templates is not None
    rs = art.get("renderSpec") or {}
    payload = art.get("payload", {}) or {}
    template_path = _template_for(art)

    ctx: dict = {
        "art": art,
        "props": rs.get("props", {}) or {},
        "payload": payload,
        "kind": rs.get("kind", ""),
        "supported_templates": sorted(SUPPORTED_TEMPLATES),
    }

    # Per-render-kind context preparation. Patterns:
    #   - kind=json-render → walk the element tree, build HTML string
    #   - kind=template → look up the right prepare_* helper by template name
    # Each prepare_* returns None on corruption → fall through to error.html
    # rather than crash the request.
    if rs.get("kind") == "json-render":
        jr_ctx = _prepare_json_render_ctx(payload)
        if jr_ctx is None:
            print(
                f"[genui][render] json-render pre-compute returned None for "
                f"id={art.get('id')} — falling back to error.html",
                flush=True,
            )
            template_path = "genui/error.html"
        else:
            ctx["jr"] = jr_ctx
    elif rs.get("kind") == "template":
        tpl_name = rs.get("template")
        prepared = None
        if tpl_name == "line_chart":
            prepared = _prepare_line_chart_ctx(payload)
            ctx_key = "chart"
        elif tpl_name == "bar_chart":
            prepared = _prepare_bar_chart_ctx(payload)
            ctx_key = "chart"
        elif tpl_name == "markdown_doc":
            prepared = _prepare_markdown_doc_ctx(payload)
            ctx_key = "doc"
        elif tpl_name == "comparison_table":
            prepared = _prepare_comparison_table_ctx(payload)
            ctx_key = "compare"
        elif tpl_name == "metric_callout":
            prepared = _prepare_metric_callout_ctx(payload)
            ctx_key = "metric"
        else:
            ctx_key = None

        if ctx_key is not None:
            if prepared is None:
                # Validator should have caught this, but if a saved artifact
                # somehow has a corrupt payload (or a renderer dep is missing),
                # fall back to the error template rather than 500ing.
                print(
                    f"[genui][render] template={tpl_name} pre-compute returned None "
                    f"for id={art.get('id')} — falling back to error.html",
                    flush=True,
                )
                template_path = "genui/error.html"
            else:
                ctx[ctx_key] = prepared

    return _templates.TemplateResponse(request, template_path, ctx)


async def page_latest(request: Request) -> Response:
    if not GENUI_ENABLED:
        return HTMLResponse("GenUI disabled", status_code=503)
    if err := _ui_guard(request):
        return err
    art = _load(request.path_params["id"])
    if not art or _is_expired(art):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<title>Not found</title>"
            "<body style='background:#0d0f14;color:#c9d1d9;font-family:monospace;padding:40px'>"
            "<h1>Artifact not found or expired</h1>"
            "<p><a style='color:#6272ff' href='/ui/saved'>Back to library</a></p>",
            status_code=404,
        )
    return _render_artifact(request, art)


async def page_saved_one(request: Request) -> Response:
    if not GENUI_ENABLED:
        return HTMLResponse("GenUI disabled", status_code=503)
    if err := _ui_guard(request):
        return err
    art = _load(request.path_params["id"])
    if not art or art.get("status") != "saved":
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<title>Not found</title>"
            "<body style='background:#0d0f14;color:#c9d1d9;font-family:monospace;padding:40px'>"
            "<h1>Saved artifact not found</h1>"
            "<p><a style='color:#6272ff' href='/ui/saved'>Back to library</a></p>",
            status_code=404,
        )
    return _render_artifact(request, art)


async def page_saved_index(request: Request) -> Response:
    if not GENUI_ENABLED:
        return HTMLResponse("GenUI disabled", status_code=503)
    if err := _ui_guard(request):
        return err
    items = _list_artifacts(status_filter="saved")
    by_date: dict[str, list[dict]] = {}
    by_category: dict[str, list[dict]] = {}
    for a in items:
        date = (a.get("createdAt", "") or "")[:10] or "unknown"
        by_date.setdefault(date, []).append(a)
        by_category.setdefault(a.get("category", "custom"), []).append(a)
    assert _templates is not None
    return _templates.TemplateResponse(
        request,
        "genui/saved_index.html",
        {
            "items": items,
            "by_date": dict(sorted(by_date.items(), reverse=True)),
            "by_category": dict(sorted(by_category.items())),
        },
    )


async def page_daily(request: Request) -> Response:
    if not GENUI_ENABLED:
        return HTMLResponse("GenUI disabled", status_code=503)
    if err := _ui_guard(request):
        return err
    items = [a for a in _list_artifacts() if a.get("category") in DAILY_CATEGORIES]
    # Latest-per-(category,date) so the page shows fresh stuff at a glance.
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for a in items:
        key = (a.get("category", ""), (a.get("createdAt", "") or "")[:10])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    assert _templates is not None
    return _templates.TemplateResponse(
        request,
        "genui/daily.html",
        {"items": deduped},
    )


# ── Registration ──────────────────────────────────────────────────────────────
def _log_startup_banner() -> None:
    """One line in deploy logs so we can verify server-side env capture
    without leaking the token. Logs ONLY length and presence flags —
    never the value itself.

    Also enumerates every GENUI_* env var actually visible to this process
    so a missing/typoed token shows up in deploy logs without needing
    container shell access.
    """
    print(
        f"[genui] enabled={'true' if GENUI_ENABLED else 'false'}"
        f" base_url_set={'true' if GENUI_BASE_URL else 'false'}"
        f" storage={GENUI_STORAGE}"
        f" ttl_hours={GENUI_TEMPORARY_TTL_HOURS}"
        f" auto_save_categories={sorted(GENUI_AUTO_SAVE_CATEGORIES)}"
        f" token_len={len(GENUI_API_TOKEN)}"
        f" token_auth={'enabled' if GENUI_API_TOKEN else 'disabled'}",
        flush=True,
    )
    # Print every GENUI_* env var the container actually has, with byte
    # length (NEVER the value). If GENUI_API_TOKEN is missing here, Railway
    # didn't inject it; if it's here with len=0, Railway injected an empty
    # string. Either way the truth is in this log line.
    genui_keys = sorted(k for k in os.environ if k.startswith("GENUI_"))
    if genui_keys:
        summary = ", ".join(f"{k}(len={len(os.environ[k])})" for k in genui_keys)
        print(f"[genui] visible_env: {summary}", flush=True)
    else:
        print("[genui] visible_env: <none — no GENUI_* vars in os.environ>", flush=True)


def get_routes(
    templates_engine: Jinja2Templates,
    guard_fn: Callable[[Request], Response | None],
) -> list[Route]:
    """Return the GenUI route list. Caller must splice this BEFORE any
    catch-all proxy route.

    Caller passes the existing Jinja engine and cookie guard so we don't
    duplicate template-loader/auth state.
    """
    global _templates, _guard
    _templates = templates_engine
    _guard = guard_fn
    _ensure_dirs()
    _log_startup_banner()
    return [
        # /list MUST come before /{id} so it isn't swallowed by the placeholder.
        Route("/api/ui/artifacts/list", api_list, methods=["GET"]),
        Route("/api/ui/artifacts", api_create, methods=["POST"]),
        Route("/api/ui/artifacts/{id}/save", api_save, methods=["POST"]),
        Route("/api/ui/artifacts/{id}", api_get, methods=["GET"]),
        Route("/api/ui/artifacts/{id}", api_delete, methods=["DELETE"]),
        Route("/ui/latest/{id}", page_latest, methods=["GET"]),
        Route("/ui/saved", page_saved_index, methods=["GET"]),
        Route("/ui/saved/{id}", page_saved_one, methods=["GET"]),
        Route("/ui/daily", page_daily, methods=["GET"]),
    ]
