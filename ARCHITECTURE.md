# Architecture

This is a Railway-deployed wrapper around [Hermes Agent](https://github.com/NousResearch/hermes-agent). Three subsystems coexist in a single container:

```
┌────────────────────────────────────────────────────────┐
│  uvicorn (server.py)                                   │
│   ├─ /setup wizard + admin API   (cookie auth)         │
│   ├─ /ui, /api/ui     (cookie OR Bearer token auth)    │  ← GenUI portal
│   ├─ /health                                           │
│   └─ catch-all reverse-proxy → 127.0.0.1:9119          │
│         │                                              │
│         ▼                                              │
│  hermes dashboard --tui   (subprocess on :9119)        │
│   └─ embedded Chat / TUI / native UI                   │
│                                                        │
│  hermes gateway           (subprocess; agent runtime)  │
│   └─ stdio MCP →  gbrain serve  (subprocess)           │
│                                                        │
│  /data/.bun/bin/gbrain    (CLI; bun-linked at boot)    │
│  /data/gbrain             (cloned fork OR rsync'd      │
│                            from /app/gbrain at boot)   │
└────────────────────────────────────────────────────────┘
```

`start.sh` runs at PID 1 (under `tini`), invokes `install_gbrain.sh` to populate the GBrain checkout, then exec's `python /app/server.py`. `server.py`'s `lifespan()` re-writes `/data/.hermes/config.yaml` (preserving user keys, injecting our managed keys) before spawning the dashboard subprocess and auto-starting the gateway.

`install_gbrain.sh` has two source modes:

- **`GBRAIN_SOURCE=remote`** (default) — git clone/update from `GBRAIN_REPO_URL` @ `GBRAIN_REF` into `/data/gbrain`. Network-dependent at boot.
- **`GBRAIN_SOURCE=local`** — rsync the vendored `./gbrain/` tree (a `git subtree` of Dbrain-hermes, baked into the image at `/app/gbrain/`) into `/data/gbrain`. No network at boot; source SHA pinned in `.gbrain-source-ref` → `/app/gbrain/.source-ref` and echoed to the boot log.

Both modes converge on the same `bun install` + `bun link` finalization, so the runtime surface is identical past that point.

## GenUI artifact portal

Code: `genui.py` + `templates/genui/*.html`. Routes added to Starlette in `server.py` *before* the catch-all proxy so `/ui/*` and `/api/ui/*` are claimed locally.

### Lifecycle

```
GBrain MCP tool   →   POST /api/ui/artifacts   →   /data/genui/artifacts/<id>.json
   (Bearer GENUI_API_TOKEN)                              │
                                                         ▼
                                            GET /ui/latest/<id>     → Jinja render
                                            POST .../{id}/save      → status: saved
                                            DELETE /api/ui/artifacts/{id}
```

Storage is one JSON file per artifact. Status field (`temporary` | `saved`) controls expiry. Lazy GC on read deletes expired temporaries.

### Template renderers (Hermes-side)

| Template          | Validator          | Renderer file                           | Notes                                     |
|-------------------|--------------------|------------------------------------------|-------------------------------------------|
| `search_table`    | generic            | `templates/genui/search_table.html`     | Tabular search results                    |
| `stats_dashboard` | generic            | `templates/genui/stats_dashboard.html`  | KPI cards + sections                      |
| `timeline_view`   | generic            | `templates/genui/timeline_view.html`    | Vertical event timeline                   |
| `jobs_status`     | generic            | `templates/genui/jobs_status.html`      | Job table with status pills               |
| `generic_cards`   | generic            | `templates/genui/generic_cards.html`    | Heterogeneous card grid                   |
| `line_chart`      | per-template       | `templates/genui/line_chart.html`       | **Hermes-side renderer** — inline SVG, no JS chart library; coords pre-computed in Python |
| `bar_chart`       | per-template       | `templates/genui/bar_chart.html`        | Phase A — vertical bars; same payload contract as `line_chart`; baseline at 0 unless data is entirely negative |
| `markdown_doc`    | per-template       | `templates/genui/markdown_doc.html`     | Phase A — markdown → safe HTML via python-markdown + bleach allowlist (tags + protocols). Raw HTML in source is stripped server-side |
| `comparison_table`| per-template       | `templates/genui/comparison_table.html` | Phase A — two-column side-by-side compare; `highlight: left\|right\|tie` styles the winner |
| `metric_callout`  | per-template       | `templates/genui/metric_callout.html`   | Phase A — single hero stat with optional delta + context |

The portal also supports `renderSpec.kind = "json-render"` (Phase C — generative UI). Payload is a tree spec `{root, elements}` with typed components from a 15-name catalog (`Container`, `Card`, `Stack`, `Grid`, `Heading`, `Paragraph`, `Metric`, `Link`, `Tag`, `Badge`, `Code`, `Quote`, `KeyValueList`, `Image`, `Divider`). Renderer in `genui.py:_render_json_render_spec` + wrapper at `templates/genui/json_render.html`. Wire-compatible with [@json-render/core](https://github.com/vercel-labs/json-render) — a future client-side React renderer is a drop-in upgrade.

A renderer is "Hermes-side" when this repo owns the rendering, regardless of which upstream component (GBrain, a cron job, manual curl) created the artifact. Adding a new template means:

1. Add the name to `SUPPORTED_TEMPLATES` in `genui.py`.
2. (Optional) Add a payload-shape validator and register it in `_TEMPLATE_PAYLOAD_VALIDATORS`.
3. (Optional) Add a context pre-compute helper and call it from `_render_artifact` for that template name.
4. Create `templates/genui/<name>.html`. Extend `_base.html` to inherit the topbar + Save/Dismiss controls.
5. Add a sample `curl` to `README.md`.
6. **Mirror on the gbrain side** — add a `TEMPLATE_CATALOG` entry in `gbrain/src/mcp/ui-middleware.ts` so the view-picker can emit it, and add a `shape*` case in `shapePortalPayload` if non-trivial input-shape coercion is needed.

### Three render paths (Phase A/B/C)

| Path | When the agent uses it | Where it lives |
|---|---|---|
| **Auto-render via UI_RULES** | The agent calls a tool (`search`, `get_stats`, `get_timeline`, `render_chart`, …) whose result has a renderable shape. Rule-based routing maps op → template. Phase A added 4 more templates (`bar_chart`, `markdown_doc`, `comparison_table`, `metric_callout`) so the view-picker has more options. | `gbrain/src/mcp/ui-middleware.ts:UI_RULES` + `templates/genui/*.html` |
| **render_response (Phase B)** | The agent's text answer would be easier to read as a rendered markdown document — headings, tables, lists, citations. The LLM calls `mcp_gbrain_render_response` inline. No second classifier LLM. | `gbrain/src/core/operations.ts:render_response` → markdown_doc template |
| **render_ui (Phase C)** | No fixed template captures the right structure. The LLM emits a full `{root, elements}` spec with typed components. Server-side renderer turns it into HTML. | `gbrain/src/core/operations.ts:render_ui` → `genui.py:_render_json_render_spec` |

When choosing among the three at design time: prefer auto-render via UI_RULES (cheapest, fully validated). Reach for `render_response` when the answer is prose-shaped. Reach for `render_ui` only when neither fits — its per-element validation is stricter and the surface is larger.

## GBrain ↔ Hermes via MCP (stdio)

`server.py:write_config_yaml` registers GBrain as an MCP stdio server in `/data/.hermes/config.yaml`:

```yaml
mcp_servers:
  gbrain:
    command: /data/.bun/bin/gbrain
    args: [serve]
    env:
      GENUI_API_TOKEN: <forwarded from os.environ>
      GENUI_BASE_URL:  <forwarded from os.environ>
      GBRAIN_DIR:      /data/gbrain
    timeout: 180
    connect_timeout: 90
```

Hermes filters subprocess env to a safe baseline (`PATH, HOME, USER, LANG, ...`); the `env:` block is the only way external env vars reach the GBrain process. Two forwarding sources are merged in `_build_gbrain_mcp_entry`:

1. **Prefix sweep** — every `GENUI_*` and `GBRAIN_*` key from `os.environ` with a non-empty value.
2. **Explicit allowlist** (`GBRAIN_EXPLICIT_FORWARD_KEYS`) — cross-prefix vars that GBrain reads but which don't carry our namespace: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCLAW_WORKSPACE`, and optional LLM-provider keys (`GOOGLE_GENERATIVE_AI_API_KEY`, `VOYAGE_API_KEY`, `GROQ_API_KEY`). Empty values are dropped, so a key being listed costs nothing until an operator actually sets it on Railway.

Tools auto-namespace as `mcp_gbrain_<tool>`.

This entry is registered when `GBRAIN_ENABLED=true` AND `/data/.bun/bin/gbrain` exists. If either is false, the entry is *removed* on the next `write_config_yaml` call so a stale entry doesn't try to spawn a missing binary. The status is logged: `[hermes-config] mcp_servers.gbrain: registered (env_forwarded=N, timeout=180s)` or `skipped (...)`.

`write_config_yaml` is read-merge-write — it preserves any user-added top-level keys (e.g. custom `mcp_servers` entries) so manual edits don't get clobbered. It runs from `lifespan()` on every server boot AND from `Gateway.start()` on every gateway start.

## Auth

| Surface                | Cookie? | Bearer token? |
|------------------------|---------|----------------|
| `/login`, `/logout`, `/health` | —       | —              |
| `/setup/*`             | required | —              |
| Reverse-proxied dashboard (`/`, `/api/*`)     | required | —              |
| `/ui/*` (artifact pages) | required | —              |
| `/api/ui/*` (artifact API) | accepted | accepted (`Authorization: Bearer $GENUI_API_TOKEN` *or* `X-Genui-Token`) |

Cookies are HMAC-signed; the secret regenerates on every process start (so any redeploy invalidates all sessions — intentional, see `server.py:17`). The Bearer-token path exists specifically so server-to-server callers (the GBrain MCP subprocess running in the same container) can post artifacts without a browser session.

## Agent-honesty rule for tool callers

When the Hermes agent (or any LLM-driven caller) invokes an MCP tool — `mcp_gbrain_*`, `mcp_gbrain_render_chart`, etc. — it must inspect the tool result for a top-level `ui` field with a `url` before claiming a UI artifact was generated.

**Required behavior:**

- If the tool result contains `ui.url`, the agent may say it created or rendered a chart/dashboard/table and reference the URL.
- If the tool result has no `ui` field (or `ui.url` is missing/empty), the agent **must not** claim it generated, rendered, or visualized anything. It should:
  - Surface the raw result (or a textual summary), and
  - State plainly that no UI artifact was produced (e.g. *"the tool returned data but did not produce a viewable artifact"*).

**Why:** the GenUI middleware lives in GBrain (`src/mcp/dispatch.ts` of the fork). When it works, it calls back into this server's `POST /api/ui/artifacts` and returns the URL alongside the data. When it doesn't work — middleware not wired, env vars missing in the subprocess, GenUI portal unreachable — the agent silently dropping that distinction makes failures look like successes and erodes trust in every later "I rendered…" claim.

This rule is documented here so it can be referenced from a Hermes system prompt or a per-session instruction. It is not currently enforced by code; it's a behavioral contract for the agent layer.
