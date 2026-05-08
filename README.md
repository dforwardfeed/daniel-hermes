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

`renderSpec.kind = "template"` with one of: `search_table`, `stats_dashboard`, `timeline_view`, `jobs_status`, `generic_cards`, `line_chart`. The other two kinds (`json-render`, `openui`) accept-and-store but render to a placeholder for now.

`line_chart` validates `payload.series[*].points[*].{x,y}` (y must be numeric) and renders inline SVG with no external charting library. See `ARCHITECTURE.md` for the renderer flow.

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
```

The `POST` response contains the shareable URL — open it in a browser to see the rendered artifact, with **Save** / **Dismiss** buttons.

## GBrain (optional)

When `GBRAIN_ENABLED=true`, `start.sh` clones `GBRAIN_REPO_URL` into `GBRAIN_DIR` (default `/data/gbrain`) and runs `bun install` + `bun link` so the `gbrain` CLI is available to Hermes.

The bun-linked binary lives at `/data/.bun/bin/gbrain`. For Hermes — including its terminal tool, gateway subprocesses, and anything spawned from the dashboard — to find it as plain `gbrain`, the directory `/data/.bun/bin` must be on `PATH`. The image handles this in three layers so all child-process and shell-spawn patterns work:

1. **`ENV PATH` in the Dockerfile** — exported into every process started from the image.
2. **`export PATH` in `start.sh` and `install_gbrain.sh`** — ensures non-login child shells inherit it explicitly.
3. **`/etc/profile.d/bun-path.sh`** — re-applies the path inside *login* shells, since `/etc/profile` on Debian otherwise resets `PATH` from scratch and would hide `gbrain` from any pty-backed terminal Hermes opens.

If you ever see `which gbrain` failing inside Hermes while `/data/.bun/bin/gbrain --version` works, one of those three layers has been bypassed.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
