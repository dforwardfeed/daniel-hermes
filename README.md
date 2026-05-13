# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **Admin Dashboard** — dark-themed UI to configure providers, channels, tools, and manage the gateway
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser
- **Live Status** — stat cards for gateway state, uptime, model, and pending pairing requests
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Basic Auth** — password-protected admin panel
- **Reset Config** — one-click reset to start fresh

## Getting Started

The easiest way to get started:

### 1. Get an LLM Provider Key (free)

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes Agent interacts entirely through messaging channels — there is no chat UI like ChatGPT. Telegram is the quickest to set up:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, and copy the **Bot Token**
3. Send a message to your new bot — it will appear as a pairing request in the admin dashboard
4. To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot)

### 3. Deploy to Railway

1. Click the **Deploy on Railway** button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to deploy logs)
3. Attach a **volume** mounted at `/data` (persists config across redeploys)
4. Open your app URL — log in with username `admin` and your password

### 4. Configure in the Admin Dashboard

1. **LLM Provider** — select OpenRouter from the dropdown, paste your API key, enter the model name
2. **Messaging Channel** — check Telegram, paste the Bot Token from BotFather
3. Click **Save & Start** — the gateway will start and your bot goes live

### 5. Start Chatting

Message your Telegram bot. If you're a new user, a pairing request will appear in the admin dashboard under **Users** — click **Approve**, and you're in.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Basic auth password — if unset, a random password is printed to logs |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn)
│   ├── /            — Admin dashboard (Basic Auth)
│   ├── /health      — Health check (no auth)
│   └── /api/*       — Config, status, logs, gateway, pairing
└── hermes gateway   — Managed as async subprocess
```

The admin server runs on `$PORT` and manages the Hermes gateway as a child process. Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## GenUI artifact portal

The server hosts a small portal that stores structured **UI artifacts** as JSON files under `/data/genui/artifacts` and renders them with server-side Jinja templates. Anything that can produce structured data — GBrain, a cron job, a chat handler — can POST an artifact and get back a shareable URL.

### Routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/ui/artifacts` | Create artifact, returns `{id, url, status}` |
| `GET` | `/api/ui/artifacts/list` | List artifacts (`?status=saved&category=…` optional) |
| `GET` | `/api/ui/artifacts/{id}` | Fetch raw artifact JSON |
| `POST` | `/api/ui/artifacts/{id}/save` | Promote temporary → saved (clears expiry) |
| `DELETE` | `/api/ui/artifacts/{id}` | Delete artifact |
| `GET` | `/ui/latest/{id}` | Render artifact in browser |
| `GET` | `/ui/saved` | Saved-library index (grouped by date / category) |
| `GET` | `/ui/saved/{id}` | Render saved artifact (404 if not saved) |
| `GET` | `/ui/daily` | Latest in `daily_briefing` / `briefing` / `stats` / `reports` |

### Render templates

`renderSpec.kind = "template"` with one of:

- **`search_table`** — tabular result list
- **`stats_dashboard`** — KPI cards + sections
- **`timeline_view`** — chronological events
- **`jobs_status`** — job-board grouped by status
- **`generic_cards`** — heterogeneous card grid (accepts `viewType: "cards"`)
- **`line_chart`** — server-side SVG line chart; validates `payload.series[*].points[*].{x,y}` (y must be numeric)
- **`bar_chart`** *(Phase A)* — server-side SVG bars; same payload contract as `line_chart`. Baseline at 0 unless data is entirely negative. Multi-series → grouped bars
- **`markdown_doc`** *(Phase A)* — markdown → safe HTML. Runs the source through python-markdown (tables / fenced_code / sane_lists / optional toc) then bleach (allowlisted tags + protocols). Raw HTML in the source is stripped server-side. Payload: `{markdown: str, summary?: str, sources?: list, toc?: bool}`
- **`comparison_table`** *(Phase A)* — two-column side-by-side. Payload: `{left: {label, sublabel?}, right: {label, sublabel?}, rows: [{label, left, right, highlight?, note?}], summary?, verdict?}` where `highlight = "left" | "right" | "tie"`
- **`metric_callout`** *(Phase A)* — single hero stat. Payload: `{value: number|string, label?, delta?, delta_kind?: "up"|"down"|"neutral", context?, footnote?, sources?}`

**`renderSpec.kind = "json-render"`** *(Phase C)* — generative UI. Payload is a tree spec `{root, elements}` where each element has `{type, props, children}`. Component catalog: `Container`, `Card`, `Stack`, `Grid`, `Divider`, `Heading`, `Paragraph`, `Code`, `Quote`, `Link`, `Metric`, `KeyValueList`, `Tag`, `Badge`, `Image`. Rendered server-side via `genui.py:_render_json_render_spec` — no React bundle, no JS. Wire format is compatible with Vercel Labs' [@json-render/core](https://github.com/vercel-labs/json-render) library so a future client-side React renderer is a drop-in upgrade. Hard caps: 500 elements, 20 nesting levels. URL props (`href`, `src`) are protocol-allowlisted to `http`, `https`, `mailto`, `tel`, or relative paths — `javascript:` and `data:` rejected at validate.

The third kind (`openui`) accepts-and-stores but renders to a placeholder for now.

### Agent-driven visualization (Phase B)

GBrain ships `render_response` — a new MCP op the LLM calls when its own text answer would be more useful as a structured markdown artifact. The agent decides; no separate classifier LLM is involved. From a Telegram (or any chat) request:

- "Summarize my notes about X" → agent calls `mcp_gbrain_render_response` with the summary as markdown → user gets a `/ui/latest/<id>` URL with the rendered doc + Save button.
- "Compare A and B" → agent calls `render_response` with a markdown comparison table.
- "Pros and cons of …" → same.

The op's tool-description tells the agent *when* to call it: more than ~3 short paragraphs, tabular data, multiple citations. Charts still go through `render_chart` (numeric-y enforcement). Single-fact replies stay as plain text.

### Generative UI (Phase C)

When neither a fixed template nor a chart nor a markdown blob captures the right structure, the agent emits a `render_ui` call with a full json-render spec. Example: a "Portfolio summary" combining four KPI metrics, a 2x2 grid of "company highlights" cards, and a footer with sources — none of the existing templates carry all three at once.

```json
{
  "root": "page",
  "elements": {
    "page": {"type": "Stack", "props": {"gap": 16}, "children": ["kpis", "highlights", "sources"]},
    "kpis": {"type": "Grid", "props": {"columns": 4}, "children": ["m1","m2","m3","m4"]},
    "m1": {"type": "Metric", "props": {"label": "AUM", "value": 128000000, "format": "currency"}},
    ...
  }
}
```

Each `type` is validated against the component catalog at the portal. URL props (`Link.href`, `Image.src`) are protocol-allowlisted. Unknown types or missing required props produce a clear `400` with the offending element id and field name — the LLM can self-correct on retry.

### Auth

Browser users hit the same cookie session as the setup wizard. For server-to-server POSTs from inside the same container (e.g. GBrain), set `GENUI_API_TOKEN` and send `Authorization: Bearer <token>`. Both auth paths are accepted; either is sufficient.

### Env vars

| Variable | Default | Description |
|---|---|---|
| `GENUI_ENABLED` | `true` | Master switch |
| `GENUI_BASE_URL` | *(infer from request)* | Used to build the `url` in POST responses |
| `GENUI_STORAGE` | `/data/genui` | Storage root |
| `GENUI_TEMPORARY_TTL_HOURS` | `72` | Temporary artifact expiry |
| `GENUI_AUTO_SAVE_CATEGORIES` | `daily_briefing,portfolio,jobs` | Categories that skip the temporary stage |
| `GENUI_API_TOKEN` | *(unset)* | Bearer token for server-to-server auth |

### Curl test (after Railway deploy)

Replace `BASE` with your Railway URL (or `GENUI_BASE_URL`) and `TOKEN` with `GENUI_API_TOKEN`:

```bash
BASE="https://your-app.up.railway.app"
TOKEN="your-genui-api-token"

# 1) Create a search_table artifact
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Hermes search results",
    "category": "search",
    "viewType": "table",
    "source": {"operation":"web_search","transport":"http","trigger":"chat"},
    "payload": {
      "query": "claude opus 4.7",
      "columns": ["rank","title","url"],
      "rows": [
        {"rank":1,"title":"Anthropic — Claude","url":"https://www.anthropic.com"},
        {"rank":2,"title":"Claude API docs","url":"https://docs.claude.com"}
      ]
    },
    "renderSpec": {"kind":"template","template":"search_table"}
  }'

# 2) Create a stats_dashboard artifact (auto-saved because category=stats? no —
#    stats is daily-surfaced but not auto-saved. To auto-save, use category=portfolio.)
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Daily portfolio snapshot",
    "category": "portfolio",
    "viewType": "dashboard",
    "source": {"operation":"portfolio_snapshot","transport":"stdio","trigger":"cron"},
    "payload": {
      "summary": "Markets up across the board.",
      "kpis": [
        {"label":"Portfolio","value":"$128,402","delta":"+1.8%","deltaLabel":"24h"},
        {"label":"Top mover","value":"NVDA","delta":"+4.2%","deltaLabel":"day"},
        {"label":"Cash","value":"$12,003","note":"available"}
      ],
      "sections": [{
        "title":"Top holdings",
        "columns":["ticker","value","change"],
        "rows":[
          {"ticker":"NVDA","value":"$42,000","change":"+4.2%"},
          {"ticker":"AAPL","value":"$31,200","change":"+0.6%"}
        ]
      }]
    },
    "renderSpec": {"kind":"template","template":"stats_dashboard"}
  }'

# 3) List
curl -sS "$BASE/api/ui/artifacts/list" -H "Authorization: Bearer $TOKEN"

# 4) Create a line_chart artifact (server-side SVG, no JS chart library)
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "MongoDB Stock Price Evolution",
    "category": "finance",
    "viewType": "graph",
    "source": {"operation":"render_chart","transport":"stdio","trigger":"chat"},
    "payload": {
      "title": "MongoDB Stock Price Evolution",
      "x_axis": "Year",
      "y_axis": "Closing Price",
      "y_format": "currency",
      "source_slug": "mongodb_data",
      "series": [{
        "name": "Closing Price",
        "points": [
          {"x":"2018","y":83.74},  {"x":"2019","y":131.61},
          {"x":"2020","y":359.04}, {"x":"2021","y":529.35},
          {"x":"2022","y":196.84}, {"x":"2023","y":408.85},
          {"x":"2024","y":232.81}, {"x":"2025","y":419.69},
          {"x":"2026 (May)","y":293.42}
        ]
      }]
    },
    "renderSpec": {"kind":"template","template":"line_chart"}
  }'

# 5) markdown_doc — prose with safe HTML sanitization
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Weekly briefing",
    "category": "briefing",
    "viewType": "markdown",
    "source": {"operation":"weekly_briefing","transport":"stdio","trigger":"cron"},
    "payload": {
      "summary": "Three things moved this week.",
      "markdown": "# Weekly briefing\n\n## Highlights\n\n- **Acme Corp** announced Q3 results — revenue up 18%\n- Federal rate decision held steady at 4.5%\n- New ML paper on long-context reasoning ([link](https://example.com/paper))\n\n## What to watch\n\n| Event | When |\n|---|---|\n| Earnings calls | Next week |\n| Fed minutes | Friday |"
    },
    "renderSpec": {"kind":"template","template":"markdown_doc"}
  }'

# 6) comparison_table — A vs B side-by-side
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "PGLite vs Postgres",
    "category": "custom",
    "viewType": "table",
    "source": {"operation":"compare","transport":"stdio","trigger":"chat"},
    "payload": {
      "summary": "Engine choice for personal-scale brains.",
      "left": {"label": "PGLite", "sublabel": "embedded"},
      "right": {"label": "Postgres", "sublabel": "managed"},
      "rows": [
        {"label": "Setup", "left": "Zero config", "right": "Service required", "highlight": "left"},
        {"label": "Concurrency", "left": "Single writer", "right": "Multi", "highlight": "right"},
        {"label": "Cost", "left": "Free", "right": "$5+/mo", "highlight": "left"}
      ],
      "verdict": "PGLite for <1000 docs; Postgres beyond that."
    },
    "renderSpec": {"kind":"template","template":"comparison_table"}
  }'

# 7) metric_callout — one big number with context
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Brain size",
    "category": "stats",
    "viewType": "dashboard",
    "source": {"operation":"get_stats","transport":"stdio","trigger":"chat"},
    "payload": {
      "label": "Pages in brain",
      "value": 1247,
      "delta": "+34 this week",
      "delta_kind": "up",
      "context": "Mostly from the weekend backfill of Q3 meeting transcripts."
    },
    "renderSpec": {"kind":"template","template":"metric_callout"}
  }'

# 8) bar_chart — vertical bars, same payload contract as line_chart
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Revenue by quarter",
    "category": "finance",
    "viewType": "chart",
    "source": {"operation":"render_chart","transport":"stdio","trigger":"chat"},
    "payload": {
      "title": "Revenue by quarter",
      "x_axis": "Quarter",
      "y_axis": "Revenue",
      "y_format": "currency",
      "series": [{
        "name": "Revenue",
        "points": [
          {"x":"Q1","y":120000}, {"x":"Q2","y":145000},
          {"x":"Q3","y":162000}, {"x":"Q4","y":189000}
        ]
      }]
    },
    "renderSpec": {"kind":"template","template":"bar_chart"}
  }'

# 9) json-render — generative UI (Phase C)
curl -sS -X POST "$BASE/api/ui/artifacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Portfolio overview",
    "category": "stats",
    "viewType": "custom",
    "source": {"operation":"render_ui","transport":"stdio","trigger":"chat"},
    "payload": {
      "root": "page",
      "elements": {
        "page":      {"type":"Stack","props":{"gap":16},"children":["intro","kpis","footer"]},
        "intro":     {"type":"Card","props":{"title":"Portfolio overview"},"children":["lead"]},
        "lead":      {"type":"Paragraph","props":{"text":"Markets recovered most of last week’s drawdown."}},
        "kpis":      {"type":"Grid","props":{"columns":3},"children":["k1","k2","k3"]},
        "k1":        {"type":"Card","props":{},"children":["m1"]},
        "m1":        {"type":"Metric","props":{"label":"AUM","value":128402,"format":"currency","delta":"+1.8%","deltaKind":"up"}},
        "k2":        {"type":"Card","props":{},"children":["m2"]},
        "m2":        {"type":"Metric","props":{"label":"Top mover","value":"NVDA","delta":"+4.2%","deltaKind":"up"}},
        "k3":        {"type":"Card","props":{},"children":["m3"]},
        "m3":        {"type":"Metric","props":{"label":"Cash","value":12003,"format":"currency"}},
        "footer":    {"type":"Paragraph","props":{"text":"Generated from get_stats at 09:00 ET.","muted":true}}
      }
    },
    "renderSpec": {"kind":"json-render"}
  }'
```

The `POST` response contains the shareable URL — open it in a browser to see the rendered artifact, with **Save** / **Dismiss** buttons.

## GBrain (optional)

When `GBRAIN_ENABLED=true`, `install_gbrain.sh` populates `GBRAIN_DIR` (default `/data/gbrain`) and runs `bun install` + `bun link` so the `gbrain` CLI is available to Hermes. There are two source modes, controlled by `GBRAIN_SOURCE`:

| Mode | Behavior | Use when |
|---|---|---|
| `remote` *(default)* | git clone/update from `GBRAIN_REPO_URL` @ `GBRAIN_REF` | Pulling the latest fork commit at every container boot (legacy behavior, requires network at boot) |
| `local` | rsync the vendored `./gbrain/` tree from inside the image | Monorepo mode — no network at boot, deterministic content, source SHA pinned in `.gbrain-source-ref` |

The bun-linked binary lives at `/data/.bun/bin/gbrain`. For Hermes — including its terminal tool, gateway subprocesses, and anything spawned from the dashboard — to find it as plain `gbrain`, the directory `/data/.bun/bin` must be on `PATH`. The image handles this in three layers so all child-process and shell-spawn patterns work:

1. **`ENV PATH` in the Dockerfile** — exported into every process started from the image.
2. **`export PATH` in `start.sh` and `install_gbrain.sh`** — ensures non-login child shells inherit it explicitly.
3. **`/etc/profile.d/bun-path.sh`** — re-applies the path inside *login* shells, since `/etc/profile` on Debian otherwise resets `PATH` from scratch and would hide `gbrain` from any pty-backed terminal Hermes opens.

If you ever see `which gbrain` failing inside Hermes while `/data/.bun/bin/gbrain --version` works, one of those three layers has been bypassed.

### Monorepo layout

The `./gbrain/` directory is a `git subtree` of the [Dbrain-hermes](https://github.com/dforwardfeed/Dbrain-hermes) fork. It ships inside the Docker image as `/app/gbrain/` and is the source for `GBRAIN_SOURCE=local` mode. The vendored fork SHA is pinned in `.gbrain-source-ref` at the repo root, copied to `/app/gbrain/.source-ref` at build time, and echoed to the boot log on every `GBRAIN_SOURCE=local` run.

To re-sync the vendored tree with upstream:

```bash
git subtree pull --prefix=gbrain https://github.com/dforwardfeed/Dbrain-hermes.git master --squash
# then update .gbrain-source-ref to the new fork HEAD SHA
git -C C:/coding-projects/Dbrain-hermes rev-parse HEAD > .gbrain-source-ref
git add .gbrain-source-ref && git commit -m "Pin gbrain source ref"
```

### Migrating Railway from remote → local mode

Cutover is reversible. The default stays `remote` so you can flip back at any time by setting `GBRAIN_SOURCE=remote` and redeploying.

1. **Deploy the monorepo image** — push the changes that introduce `./gbrain/`, the `COPY gbrain/` Dockerfile step, and `rsync` in apt. With `GBRAIN_SOURCE` unset (or `=remote`), runtime behavior is identical to before.
2. **Create a Railway preview environment** — fork from production so it inherits all env vars, then change just `GBRAIN_SOURCE=local` on the preview. Trigger a redeploy.
3. **Run the verification checklist** (below) against the preview URL.
4. **Add any cross-prefix env vars** that GBrain needs but aren't in your service env yet. The forward allowlist in `server.py:GBRAIN_EXPLICIT_FORWARD_KEYS` covers `DATABASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCLAW_WORKSPACE`, and a few optional LLM-provider keys — but the *values* must be set on Railway for them to do anything. The forward is a no-op when a key is unset.
5. **Flip prod** — once the preview is green, set `GBRAIN_SOURCE=local` on the production environment.
6. **Keep `GBRAIN_REPO_URL` / `GBRAIN_REF` set** for at least one week after cutover. Rollback is just `GBRAIN_SOURCE=remote` + redeploy.
7. **After a clean week**, archive the `Dbrain-hermes` repo on GitHub so stale CI never runs against a dormant tree.

### Verification checklist (run against the preview URL first)

- [ ] Railway build succeeds (first build after the apt-get rsync addition is slower — the layer cache is busted, hermes-agent + ui-tui rebuild)
- [ ] `GET /health` returns `{"status":"ok",...}`
- [ ] `GET /setup` loads the admin dashboard
- [ ] Boot logs show `[install_gbrain] Source: local` and `Source ref: 48b0db1c…`
- [ ] Boot logs show `[hermes-config] mcp_servers.gbrain: registered (env_forwarded=N, timeout=180s)`
- [ ] Hermes dashboard Chat tab opens (PTY connects via `/api/pty`)
- [ ] `mcp_gbrain_*` tools respond to a chat message
- [ ] `POST /api/ui/artifacts` from a `render_chart` MCP call returns a `/ui/latest/<id>` URL that renders the line_chart SVG

### Env vars (GBrain-side)

| Variable | Default | Mode | Description |
|---|---|---|---|
| `GBRAIN_ENABLED` | `false` | both | Master switch — set to `true` to enable the GBrain MCP server |
| `GBRAIN_SOURCE` | `remote` | both | `remote` (git clone) or `local` (rsync vendored `/app/gbrain/`) |
| `GBRAIN_REPO_URL` | `https://github.com/dforwardfeed/Dbrain-hermes.git` | remote | Git URL to clone |
| `GBRAIN_REF` | `main` | remote | Ref to check out. **Note**: the fork's primary branch is `master`, not `main` — set this in Railway explicitly |
| `GBRAIN_DIR` | `/data/gbrain` | both | Where the runtime checkout lives (must be on the persistent volume) |
| `GBRAIN_LOCAL_SOURCE_DIR` | `/app/gbrain` | local | Where the in-image vendored tree lives |
| `GBRAIN_REQUIRED` | `false` | both | If `true`, install failures abort container boot |

## Custom user-defined views

Beyond the three built-in surfaces (Latest / Saved / Daily), the GenUI portal
supports **user-defined views** — persistent named sections you create via
chat. Today's only kind is `checklist`: a to-do-style list with strikethrough,
inline checkbox interactivity, and per-item add/remove. Each view appears at
`/ui/view/<slug>` and shows up in the shared topbar nav across every page.

The agent manages views via nine MCP tools (auto-namespaced as
`mcp_genui_*` once registered): `list_views`, `create_view` (with optional
`template` for built-in scaffolds), `delete_view`, `add_item`, `mark_done`,
`remove_item`, `edit_item` (fix typos, change notes), `set_due` (deadline
chips), `export_markdown` (copy the list out as plain markdown). Activation
requires only `GENUI_API_TOKEN` (already set for the artifact API) — the
server registers the `genui` MCP entry automatically.

Try in Telegram:

- *"Create a view called todo for my to-do list."*
- *"Create a daily-plan view for today."* — scaffold seeds three reflection prompts.
- *"Add 'call accountant' to my todo, due Friday."* — agent resolves "Friday" to ISO before calling `mcp_genui_add_item` + `mcp_genui_set_due`. Overdue items float to the top with a red chip.
- *"What's on my todo list?"*
- *"Fix the typo in 'call accountnt' to 'call accountant'."* — `mcp_genui_edit_item`.
- *"Mark 'call accountant' as done."*
- *"Export my todo list as markdown so I can paste it into Slack."* — `mcp_genui_export_markdown`.

**Browser quick-add:** every GenUI page (artifact view, Library, Daily, Views index, individual view) shows a floating `+` button in the bottom-right. Click it → pick a view → type the item → Enter. Keyboard shortcut `Alt+A`. Adds happen via the same `/api/ui/views/<slug>/items` endpoint the agent uses, so the agent sees the new item next time you ask.

**Built-in view scaffolds** (pass `template` on create_view): `daily-plan` (3 reflection prompts), `weekly-review` (4 end-of-week questions), `decision-log` (3 prompts per decision), `reading-list` (empty with a description), `groceries` (empty with a description).

Checkboxes on the rendered view are interactive — click in the browser to
toggle done state; the agent and the UI share the same source of truth via
`/api/ui/views/<slug>/items/<id>` (cookie OR bearer auth, same as
`/api/ui/artifacts`).

| Variable | Default | Description |
|---|---|---|
| `GENUI_API_TOKEN` | *(unset)* | Required for `mcp_genui_*` tools. Same token as the existing artifact API. |
| `GENUI_VIEWS_DIR` | `$GENUI_STORAGE/views` | Where view JSON files persist (one per slug). Survives Railway redeploys when GENUI_STORAGE is on `/data`. |

Views are stored as plain JSON files at `/data/genui/views/<slug>.json` —
human-readable, hand-editable, and version-controllable. The MCP subprocess
calls back into the same-container Starlette server via loopback, so no
external network is involved.

## Constellation (read-only YouTube-insight library)

Hermes can also query the user's **Constellation** library — a separate app
that summarizes YouTube videos and stores the user's saved insights under
two parallel Brains (Original = life/business; AI = AI research). The
integration ships as a stdio MCP server (`constellation_mcp.py`) registered
alongside GBrain.

Set both env vars on Railway to activate it; leave them unset to disable:

| Variable | Default | Description |
|---|---|---|
| `CONSTELLATION_BASE_URL` | *(unset)* | The deployed Constellation API root, e.g. `https://your-constellation.replit.app`. No trailing slash. |
| `CONSTELLATION_API_TOKEN` | *(unset)* | The Constellation `AGENT_API_TOKEN` secret. Sent as `Authorization: Bearer <token>` on every API call. Forwarded to the MCP subprocess and never logged. |
| `CONSTELLATION_TIMEOUT` | `30` | HTTP request timeout in seconds. |

When both are present, the boot log shows:
`[hermes-config] mcp_servers.constellation: registered (env_forwarded=N, timeout=60s)`.
Hermes namespaces the tools as `mcp_constellation_*`. Six tools are exposed:
`categories`, `search`, `semantic_search`, `library`, `library_all`, `get_video`.

Ask Hermes naturally — *"What's my Brain saying about agent memory?"*,
*"Show me my Sales & GTM insights"*, *"Find every block that mentions PMF"* —
and it picks the right tool. The full API spec is documented at
`<CONSTELLATION_BASE_URL>/api/agent/manifest`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
