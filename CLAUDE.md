# Claude Code — operating notes for this repo

This file is the entry point for any AI agent (or fresh contributor) working in `daniel-hermes`. Read it before anything else. For the user-facing overview see `README.md`; for the deeper system map see `ARCHITECTURE.md`.

## What this repo is

`daniel-hermes` is a **monorepo** with two scopes living side by side:

1. **Hermes wrapper** (everything at the root) — a Railway-deployed Python admin server that runs the [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway as a subprocess, fronted by an admin dashboard and a GenUI artifact portal. This is the Railway deploy target.

2. **`gbrain/`** — a `git subtree` of [Dbrain-hermes](https://github.com/dforwardfeed/Dbrain-hermes), the GBrain MCP server fork that Hermes consumes over stdio. Source SHA pinned in `.gbrain-source-ref`. Edit it as normal repo code; understand the subtree workflow before pulling upstream updates.

**Rule of thumb**: if your task involves Docker, Python, the admin server, the gateway, or the GenUI portal → root files. If it involves MCP tools, ingestion, search, embeddings, view-picker logic → `gbrain/` files.

## File map

```
daniel-hermes/
├── CLAUDE.md                       this file
├── README.md                       user-facing overview, env vars, curl examples
├── ARCHITECTURE.md                 system map — three subsystems, boot flow, auth
│
├── Dockerfile                      single-container image; builds hermes-agent + ui-tui at build time
├── start.sh                        PID-1 entrypoint (under tini) — calls install_gbrain.sh, exec's server.py
├── install_gbrain.sh               GBrain installer; two modes (remote/local) — see "Boot flow" below
├── .gbrain-source-ref              pins the vendored gbrain/ subtree SHA → /app/gbrain/.source-ref at build
├── .dockerignore                   trims Docker build context (don't add gbrain/admin/dist here — it's needed)
├── railway.toml                    Railway build + deploy config (healthcheck = /health)
├── requirements.txt                Python deps for the admin server
│
├── server.py                       THE admin server. Starlette app on $PORT. Owns:
│                                     - /setup wizard + admin API (cookie auth)
│                                     - /login, /logout, /health
│                                     - GenUI route splice (before catch-all)
│                                     - reverse proxy to 127.0.0.1:9119 (hermes dashboard)
│                                     - WebSocket proxy for /api/pty, /api/ws, /api/events
│                                     - Gateway + Dashboard subprocess managers
│                                     - _build_gbrain_mcp_entry: how GBrain is registered with Hermes
│
├── genui.py                        GenUI portal. Artifact storage, validation, rendering dispatch.
│                                     - Validators: VALID_VIEW_TYPES, SUPPORTED_TEMPLATES, etc.
│                                     - Auth helpers: cookie OR Bearer token on /api/ui/*
│                                     - Per-template validators (line_chart shape) and pre-compute helpers
│
├── templates/                      Jinja templates served by server.py
│   ├── index.html                  admin dashboard
│   └── genui/                      GenUI renderers — one HTML file per template name
│       ├── _base.html              shared topbar + Save/Dismiss controls
│       ├── search_table.html
│       ├── stats_dashboard.html
│       ├── timeline_view.html
│       ├── jobs_status.html
│       ├── generic_cards.html
│       └── line_chart.html         server-side SVG; coords pre-computed in genui.py
│
└── gbrain/                         vendored subtree (see "Subtree workflow" below)
    ├── CLAUDE.md                   gbrain's OWN instructions — scoped to working *inside* gbrain
    ├── ARCHITECTURE.md             gbrain's architecture
    ├── README.md                   gbrain's readme
    ├── src/                        gbrain source (TypeScript, bun)
    │   ├── mcp/
    │   │   ├── server.ts           MCP server entry — bun link target
    │   │   ├── dispatch.ts         tool dispatch + GenUI hook
    │   │   └── ui-middleware.ts    GenUI engine: UI_RULES, view-picker, payload shaping, artifact POST
    │   └── core/operations.ts      MCP ops (including render_chart)
    ├── admin/dist/                 pre-built React SPA bundle (DO commit; bun --compile embeds it)
    ├── package.json, bun.lock      bun-managed deps
    └── ...                         many more — gbrain is a substantial project
```

## How the UI works — three distinct layers

When making UI changes, the first question is **which layer**:

| Layer | What it is | Where to edit |
|---|---|---|
| **Admin dashboard** | `/setup` wizard, config UI, log viewer — our code | `templates/index.html`, `server.py` admin routes |
| **GenUI portal** | `/ui/*`, `/api/ui/*` — renders structured artifacts (charts, tables, dashboards) posted by anything in the container | `templates/genui/<name>.html` + `genui.py` (validator + per-template helpers) |
| **Hermes native dashboard** | `/` and most other paths — reverse-proxied to the `hermes dashboard --tui` subprocess on `:9119`. Not our code. | Upstream — file an issue at NousResearch/hermes-agent, or override locally via a separate route claim in `server.py` *before* the catch-all proxy |

**Adding a new GenUI template** (the most common UI task): the recipe is in `ARCHITECTURE.md` under "Template renderers", and a curl example for each existing template is in `README.md`. Summary: add to `SUPPORTED_TEMPLATES` in `genui.py`, optionally add a payload-shape validator (`_TEMPLATE_PAYLOAD_VALIDATORS`) and a context pre-compute helper (called in `_render_artifact`), write `templates/genui/<name>.html` extending `_base.html`. **Then mirror on the gbrain side**: add an entry to `TEMPLATE_CATALOG` in `gbrain/src/mcp/ui-middleware.ts` so the view-picker can emit it, and add a `shape*` case in `shapePortalPayload` for any non-trivial input-shape coercion.

**Current template catalog** (10 templates, all server-side Jinja + optional Python pre-compute):
`search_table`, `stats_dashboard`, `timeline_view`, `jobs_status`, `generic_cards`, `line_chart`, `bar_chart`, `markdown_doc`, `comparison_table`, `metric_callout`. The `markdown_doc` template runs python-markdown through bleach with an explicit allowlist — never disable that sanitization; the LLM emits the markdown source.

**Agent-driven render path (Phase B)**: gbrain's `render_response` MCP op (in `gbrain/src/core/operations.ts`) lets the LLM wrap its own text response in a `markdown_doc` artifact when prose-shaped answers would be easier to read as a rendered document. UI_RULES entry in `gbrain/src/mcp/ui-middleware.ts` routes it through GenUI automatically. No second LLM classifier needed — the model that wrote the response is the one deciding to call `render_response`.

**Artifact lifecycle:** anything inside the container can `POST /api/ui/artifacts` with `Authorization: Bearer $GENUI_API_TOKEN`. Artifacts are stored as one JSON file per id under `/data/genui/artifacts/`. Status starts `temporary` (72h TTL by default) or `saved` (if category in `GENUI_AUTO_SAVE_CATEGORIES`). Lazy GC on read deletes expired temporaries.

## Boot flow

`start.sh` runs at PID 1 under `tini`:
1. Exports `BUN_INSTALL=/data/.bun` and PATH.
2. Creates the `/data/.hermes/*` directories Hermes expects.
3. Removes any stale `/data/.hermes/gateway.pid` (necessary because the persistent volume survives crashes).
4. Runs `install_gbrain.sh`. Depending on `GBRAIN_SOURCE`:
   - **`remote` (default)** — git clone/update from `GBRAIN_REPO_URL` @ `GBRAIN_REF` into `/data/gbrain`.
   - **`local`** — rsync the vendored `/app/gbrain/` tree into `/data/gbrain` (`--delete`, excluding `node_modules`, `admin/node_modules`, `.git`). Logs the pinned SHA from `/app/gbrain/.source-ref`.
   Both paths then run `bun install` + `bun link` so `gbrain` resolves on PATH at `/data/.bun/bin/gbrain`.
5. Exec's `python /app/server.py`.

`server.py`'s `lifespan()` then:
- Re-writes `/data/.hermes/config.yaml` (preserving user keys, injecting our managed keys including `mcp_servers.gbrain`).
- Spawns the hermes dashboard subprocess.
- Auto-starts the hermes gateway if `is_config_complete()`.

## Production deploy

- **Railway watches `main`** of this repo. Push = auto-deploy.
- **Image is single-container.** ~501 MB. The hermes-agent React dashboard and ui-tui Node bundle are pre-built at image-build time so first request is instant.
- **Persistent volume** at `/data` — Hermes config, gateway PID, GenUI artifacts, gbrain checkout, bun-link target all live here.
- **Healthcheck** `/health` (5-minute window). Returns 200 even if the gateway is stopped — the admin server itself is up.
- **Rollback** for the local↔remote migration: set `GBRAIN_SOURCE=remote` in Railway env, trigger redeploy. The clone path takes over again because `GBRAIN_REPO_URL` and `GBRAIN_REF` are still set.

## Subtree workflow (gbrain/)

You can freely edit files inside `gbrain/` and commit them to `daniel-hermes` main. Two situations to know about:

**Re-syncing with upstream** (when Dbrain-hermes gets a useful update):
```bash
git subtree pull --prefix=gbrain https://github.com/dforwardfeed/Dbrain-hermes.git master --squash
# update the pin
git -C /path/to/Dbrain-hermes-clone rev-parse HEAD > .gbrain-source-ref
git add .gbrain-source-ref && git commit -m "Pin gbrain source ref"
```
If you've edited gbrain/ files locally between syncs, the merge may conflict. Resolve like any merge conflict.

**Pushing changes back upstream** — `git subtree push --prefix=gbrain <remote> <branch>`. Don't do this unless explicitly asked. Most fork-specific edits stay fork-only.

## Auth model — quick reference

| Surface | Cookie | Bearer token |
|---|---|---|
| `/health`, `/login`, `/logout` | — | — |
| `/setup/*` | required | — |
| Reverse-proxied dashboard (`/`, `/api/*`) | required | — |
| `/ui/*` (artifact pages) | required | — |
| `/api/ui/*` (artifact API) | accepted | accepted (`Authorization: Bearer $GENUI_API_TOKEN` or `X-Genui-Token`) |

Cookie secret regenerates on every process start — any `ADMIN_PASSWORD` change → redeploy → all sessions invalidate. The Bearer-token path exists specifically so server-to-server callers (the GBrain MCP subprocess in the same container) can post artifacts without a browser session. That's why `_build_gbrain_mcp_entry` in `server.py` forwards `GENUI_API_TOKEN` into the subprocess env.

## Env forwarding to GBrain

`server.py:_build_gbrain_mcp_entry` is the only way env vars reach the gbrain subprocess (Hermes filters subprocess env to a safe baseline). Two sources merged:

1. **Prefix sweep** — every `GENUI_*` / `GBRAIN_*` with a non-empty value.
2. **Explicit allowlist** (`GBRAIN_EXPLICIT_FORWARD_KEYS`) — cross-prefix vars gbrain needs: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCLAW_WORKSPACE`, optional provider keys.

If gbrain is "not seeing" an env var, this is the first place to look.

## Conventions

- **Commit size**: large commits are fine when the change is coherent. Don't over-split unless the user asks or risk warrants staging (e.g. a risky migration).
- **Pushing**: the user runs `git push` themselves. Surface a ready-to-paste `cd C:\coding-projects\daniel-hermes; git push` block whenever a local commit lands.
- **Auto-deploy**: every push to `main` deploys to production immediately. Smoke-test (`/health`, `/setup`, Telegram) after every deploy.
- **Risky changes**: gate behind env vars with the safe default unchanged. The `GBRAIN_SOURCE=remote|local` pattern is the template — old behavior remains the default, new behavior is opt-in via env, rollback is a single env-var flip.

## Common gotchas

- **`gbrain/admin/dist/` IS tracked and required.** It's the pre-built React bundle gbrain embeds via `bun --compile`. Never add it to `.dockerignore` or `.gitignore`.
- **`GBRAIN_REF` default is `main`** but the Dbrain-hermes fork's branch is `master`. Railway env must override this for remote mode to work. If you ever see `git fetch origin main` failing in install logs, this is it.
- **Cookie secret regenerates per boot** — `secrets.token_bytes(32)` at module load. Surprises someone every time. It's intentional (`server.py:339`).
- **`/data/.hermes/gateway.pid`** survives container restarts because `/data` is the persistent volume. `start.sh` removes it unconditionally before boot. Don't remove that line — you'll get `PID file race lost to another gateway instance` errors.
- **GenUI agent-honesty rule** — the LLM should only claim it "rendered" an artifact when the tool result actually contains `ui.url`. See `ARCHITECTURE.md` "Agent-honesty rule" section. Not enforced in code; it's a system-prompt-level contract.

## Pointers for deeper reading

- **Full architecture**: `ARCHITECTURE.md` (especially the three-subsystem ASCII diagram, the MCP server config, and the auth table).
- **User-level setup + curl examples**: `README.md`.
- **GBrain-side guidance** (when working inside `gbrain/`): `gbrain/CLAUDE.md`, `gbrain/AGENTS.md`, `gbrain/ARCHITECTURE.md` — but treat those as scoped to gbrain's project, not as guidance for the daniel-hermes wrapper.
