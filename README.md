# Signa

FastAPI backend for prosthetic assessment: R2 scan uploads, report data, and LiveKit voice intake.

## Local development

### Web API

```bash
uv sync
PORT=8000 uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $PORT
```

### Agent worker (required for voice intake)

```bash
uv run python -m app.agent.intake dev
```

### Both processes (Procfile)

```bash
honcho start
```

### signa-api frontend

The patient UI lives in the sibling [`signa-api`](../signa-api) repo. Point it at this API by setting the token fetch base URL in `signa-api/public/app.js`:

```js
const API_BASE = 'http://localhost:8000';
// fetch(`${API_BASE}/api/livekit/token?room=...&identity=...`)
```

Then run the frontend only:

```bash
cd ../signa-api && npm run dev
```

## Environment variables

Copy `.env.example` to `.env`. See `.env.example` for R2, LiveKit, and Gemini keys.

| Variable | Used by |
|----------|---------|
| `AWS_*` | Web API (`/datos_reporte`, `/escaneo`) |
| `LIVEKIT_*` | Web API (token) + agent worker |
| `GEMINI_API_KEY` | Agent worker only |
| `CORS_ORIGINS` | Web API (default `http://localhost:3000`) |

## Railway deployment

Deploy **two services** from this repo:

| Service | Dockerfile | CMD |
|---------|------------|-----|
| Web API | `Dockerfile` | `uvicorn app.main:app` |
| Agent worker | `Dockerfile.agent` | `python -m app.agent.intake start` |

Both services need `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`. The agent service also needs `GEMINI_API_KEY`. Set `CORS_ORIGINS` on the web service to your signa-api frontend URL in production.

### Procfile (Honcho / Railway)

```bash
honcho start
```

Process types in [`Procfile`](Procfile): `web` (FastAPI) and `agent` (LiveKit worker). The web process sets `PORT=8000` to avoid conflicting with macOS AirPlay on port 5000.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/livekit/token?room=&identity=` | Mint LiveKit token + dispatch `intake-agent` |
| `POST` | `/api/sessions` | Create session ID |
| `GET` | `/datos_reporte` | Report data with presigned model URLs |
| `POST` | `/escaneo` | Upload scan file to R2 |
