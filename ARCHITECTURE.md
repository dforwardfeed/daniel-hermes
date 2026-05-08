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
│  /data/gbrain             (cloned fork, master branch) │
└────────────────────────────────────────────────────────┘
```

`start.sh` runs at PID 1 (under `tini`), invokes `install_gbrain.sh` to clone/update the GBrain fork, then exec's `python /app/server.py`. `server.py`'s `lifespan()` re-writes `/data/.hermes/config.yaml` (preserving user keys, injecting our managed keys) before spawning the dashboard subprocess and auto-starting the gateway.

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

A renderer is "Hermes-side" when this repo owns the rendering, regardless of which upstream component (GBrain, a cron job, manual curl) created the artifact. Adding a new template means:

1. Add the name to `SUPPORTED_TEMPLATES` in `genui.py`.
2. (Optional) Add a payload-shape validator and register it in `_TEMPLATE_PAYLOAD_VALIDATORS`.
3. (Optional) Add a context pre-compute helper and call it from `_render_artifact` for that template name.
4. Create `templates/genui/<name>.html`. Extend `_base.html` to inherit the topbar + Save/Dismiss controls.
5. Add a sample `curl` to `README.md`.

Don't build a generic chart-library frontend until two more chart types are needed. A single `<svg>` keeps deploys deterministic and avoids the React/JS bundle drift that `openui` would invite.

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

Hermes filters subprocess env to a safe baseline (`PATH, HOME, USER, LANG, ...`); the `env:` block is the only way `GENUI_*` and `GBRAIN_*` reach the GBrain process. Tools auto-namespace as `mcp_gbrain_<tool>`.

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
