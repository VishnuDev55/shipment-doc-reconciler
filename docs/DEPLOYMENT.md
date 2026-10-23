# Deployment

Three ways to get this running somewhere other than your laptop, fastest first.
No live hosted URL exists for this submission — see "Public URL status" at the
bottom before you cite one.

## 1. One command, local

```bash
docker compose up --build     # http://localhost:8000
```

`docker-compose.yml` puts `runs/` in a named volume, because the filesystem *is*
the database here: `report.json`, the page JPEGs, the uploads and the raw model
bodies all live under it, and losing the container layer loses your history.

With no keys set, add `DEMO_MODE=true` to the environment or edit the compose
file; the bundled cache replays real captured model responses.

For a bare run without compose:

```bash
docker build -t shipment-doc-reconciler .
docker run -p 8000:8000 -v sdr-runs:/app/runs shipment-doc-reconciler
```

The image is single-stage `python:3.11-slim` with **no `apt-get`** — every
dependency resolves to a binary wheel (audited in the Dockerfile header, and the
build itself now verified to exit 0). It runs as an unprivileged `appuser`, and
`/app/runs` is the only path it needs to write.

## 2. Render (the committed Blueprint)

`render.yaml` is a Render Blueprint. Connect the repository in the Render
dashboard and it creates the service with the right Dockerfile, health-check
path and environment.

The committed blueprint is the **keyless demo**:

```yaml
DEMO_MODE: "true"          # replays the bundled cache
GUI_CONFIG_ENABLED: "false"  # no anonymous writes to process-global keys
API_KEYS: ""               # set this before exposing the API to anyone
RUNS_DIR: /tmp/shipment-runs # ephemeral on the free tier, by design
```

Two things to know before you change it:

- **`GUI_CONFIG_ENABLED` must stay false on any public instance.** The Configure
  drawer writes keys into the running process and there is no auth or session, so
  the last visitor to save a key sets it for everyone. Locally that is the point;
  publicly it is an open door. Put real keys in the dashboard's encrypted env
  vars instead.
- **`RUNS_DIR` on the free tier is ephemeral.** Deploys and restarts erase run
  history. The reports themselves are self-contained (the ZIP carries the pages
  and raw bodies), so nothing is lost that a reviewer would see, but a shared
  deployment needs a persistent disk.

To make it a live instance: set `NVIDIA_API_KEY` and `NEBIUS_API_KEY` as secret
env vars in the dashboard, flip `DEMO_MODE` to `"false"`, and leave
`GUI_CONFIG_ENABLED` false. Do not put real keys in `render.yaml`.

The exact steps, in order:

1. Push the repo to GitHub.
2. Render dashboard → **New → Blueprint** → point at the repo. Render reads
   `render.yaml` and creates the web service (Docker, free plan, health path
   `/api/health`).
3. The image binds `$PORT`, which Render injects — the Dockerfile's
   `CMD ... --port ${PORT:-8000}` exists so the platform's router can reach the
   app instead of it sitting on a dead 8000.
4. Open the `.onrender.com` URL. On a free instance it sleeps after 15 idle
   minutes and takes ~60s to wake, so the first request may show Render's
   loading page — that is the platform waking, not the app crashing.

### Post-deploy checklist

| Step | What to do | What you are looking at |
|---|---|---|
| 1. Cold start | Open the URL (twice if it slept) | The demo banner **"Demo mode: replaying captured real model output"** at the top, and no script error |
| 2. Health | `curl <url>/api/health` | `"status":"ok"`, `"mode":"demo"`, `"version":"1.0.0"`, a non-empty `git_commit`, both providers `configured:false` |
| 3. Conflict demo | Click the **sample shipment** that contradicts itself (sidebar → `shipment_conflict`) | The verdict panel, a **Blocked**/**Review needed** banner, and the comparison table with the disagreement quoted |
| 4. Export | **Download ZIP** on the settled report | `report.html` opens offline, `report.json` parses, `report.pdf` renders, `pages/` has the evidence images |
| 5. Live switch | (optional) set the two keys as dashboard secrets, `DEMO_MODE=false`, redeploy | Step 2 now shows `"mode":"live"` and `configured:true`; a real upload makes outbound calls to NVIDIA + Nebius |

## 3. Anywhere else (Fly, Railway, a VPS)

Any host that can build a Dockerfile will run this. There is nothing
platform-specific: no Postgres, no Redis, no queue, no build step.

```bash
fly launch --dockerfile Dockerfile          # then set the env vars below
railway up                                   # detects the Dockerfile
```

The complete environment surface is in `.env.example`. Minimum for live use:

| Variable | Value |
|---|---|
| `NVIDIA_API_KEY` | from <https://build.nvidia.com/settings/api-keys> |
| `NEBIUS_API_KEY` | from <https://tokenfactory.nebius.com/project/api-keys> |
| `DEMO_MODE` | `false` |
| `GUI_CONFIG_ENABLED` | `false` on anything reachable by others |
| `RUNS_DIR` | a persistent path |

## Health and readiness

`GET /api/health` returns 200 whenever the app is serving, **including when no
key is configured**. That is deliberate: the container's `HEALTHCHECK` must not
get an unconfigured instance killed, and the app is genuinely useful in
`DEMO_MODE`. Provider readiness is reported inside the body instead:

```json
{
  "status": "ok",
  "mode": "demo",
  "version": "1.0.0",
  "git_commit": "3accaf8... or unknown",
  "vision":  {"provider": "nvidia", "model": "...", "host": "integrate.api.nvidia.com", "configured": false},
  "summary": {"provider": "nebius", "model": "...", "host": "api.tokenfactory.nebius.com", "configured": false, "enabled": true},
  "models":  [{"role": "vision", "status": "unverified", "note": "no key for this host…"}]
}
```

`mode` is `demo` or `live`; `version` and `git_commit` say which build is
answering, which is how you tell a stale deploy from a broken one. Provider
readiness is `configured: true/false` — a boolean, never the key, because a
health endpoint is the most likely thing on a server to get scraped.

Readiness is therefore something a caller reads, not something the endpoint
refuses with. If you want a strict liveness signal for an orchestrator, gate on
`vision.configured` yourself.

## CI

`.github/workflows/ci.yml` runs the offline suite on Python 3.11 and 3.12, lints,
then builds the image and smoke-tests `/api/health`, a fixture run and
`/api/config` inside the container.

The suite cannot reach the network from CI: `tests/conftest.py` refuses every
non-loopback socket and `tests/test_offline_guard.py` fails if that wall stops
standing. So a CI run spending a provider key is not a risk to manage, it is a
thing the tests refuse to do.

**Status: written, not observed.** An `origin` remote is configured
(`github.com/VishnuDev55/shipment-doc-reconciler`, currently empty), but no push
has landed on it from this machine, so the workflow has never executed. The same
steps were run locally instead, and are recorded in `docs/AUDIT.md`.

## Public URL status

**There is no hosted instance running.** No deploy has been executed against
Render, Fly or Railway from this environment; there are no deploy credentials
here, and nothing has been pushed to the configured remote. Any statement that a
live URL exists would be false, so none is made. The submitted artifact is the
source plus a container image that builds and runs.
