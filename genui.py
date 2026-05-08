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
VALID_VIEW_TYPES = {"dashboard", "table", "graph", "timeline", "document", "status", "custom"}
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


# Map template name → optional payload validator. Adding a new entry here
# wires per-template validation without touching _validate_create.
_TEMPLATE_PAYLOAD_VALIDATORS: dict[str, "Callable[[dict], list[str]]"] = {
    "line_chart": _validate_line_chart_payload,
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
        errs.append(f"category must be one of {sorted(VALID_CATEGORIES)}")

    view_type = body.get("viewType", "custom")
    if view_type not in VALID_VIEW_TYPES:
        errs.append(f"viewType must be one of {sorted(VALID_VIEW_TYPES)}")

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


# ── UI handlers ───────────────────────────────────────────────────────────────
def _template_for(art: dict) -> str:
    rs = art.get("renderSpec", {}) or {}
    kind = rs.get("kind")
    if kind == "template":
        name = rs.get("template", "")
        if name in SUPPORTED_TEMPLATES:
            return f"genui/{name}.html"
    # json-render / openui / unknown all fall through to a placeholder.
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

    # Per-template context preparation. Add an entry here when a template
    # needs computed view-model state (e.g. SVG coords for line_chart).
    if rs.get("kind") == "template" and rs.get("template") == "line_chart":
        chart_ctx = _prepare_line_chart_ctx(payload)
        if chart_ctx is None:
            # Validator should have caught this, but if a saved artifact
            # somehow has a corrupt payload, fall back to the error template
            # rather than 500ing.
            template_path = "genui/error.html"
        else:
            ctx["chart"] = chart_ctx

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
