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

**Status: observed on GitHub, and it went red before it went green.** The first
run ever executed (2026-10-23, commit `bc61e92`, run 39914486048) failed all four
Python jobs — and passed `docker` — because `requirements-dev.txt` pinned
`pytest==9.1.1` while `pytest-playwright==0.7.1` declares `pytest<9`, so a fresh
clone could not resolve the dev extras at all. The suite died in dependency
resolution before running a single test; nothing in `app/` was implicated, and the
local run never saw it because this machine has 8.4.2 installed. Pinning pytest to
`8.4.2` (commit `bf258db`) fixed it, verified by a clean-venv dry run before
pushing rather than by spending another CI attempt.

Run 39915598051 on `bf258db` is the observed green: `lint-and-test` on 3.11 and
3.12, the coverage floor, the chromium `DEMO_MODE` browser suite, and the docker
build with its smoke test and `HEALTHCHECK` — five jobs, all `success`.

The same steps had been run locally beforehand and are recorded in
`docs/AUDIT.md`: 1005 offline tests, 28 browser tests, ruff and pyflakes clean,
mypy at its 8-error baseline.

## Public URL status

**There is no hosted instance running.** No deploy has been executed against
Render, Fly or Railway from this environment; there are no deploy credentials
here, and nothing has been pushed to the configured remote. Any statement that a
live URL exists would be false, so none is made. The submitted artifact is the
source plus a container image that builds and runs.


## The container

```bash
docker build -t shipment-doc-reconciler .
docker run -p 8000:8000 -v sdr-runs:/app/runs shipment-doc-reconciler   # live; paste keys into Configure
docker compose up --build                                               # same, with a named runs/ volume
docker run -p 8000:8000 -e NVIDIA_API_KEY=... -e NEBIUS_API_KEY=... \
  shipment-doc-reconciler                                  # live, keys from the environment
docker run -p 8000:8000 -e DEMO_MODE=true shipment-doc-reconciler   # keyless cache demo
```

The image declares a `HEALTHCHECK` and runs as an unprivileged user. The whole
surface is HTTP: [`API.md`](API.md) is the endpoint reference, the `API_KEYS`
contract, and working curl / Python / JavaScript clients for a run.

Mount `runs/` (or set `RUNS_DIR` to a persistent path). The filesystem is the
database: `report.json`, the page JPEGs, the uploads and the raw model bodies all
live under it.

The image needs **no `apt-get`**: all 69 transitive dependencies resolve to
binary manylinux wheels, and every shared-library dependency is either vendored
inside a wheel or on the `manylinux_2_28` whitelist. This was audited with
`readelf`/`auditwheel` rather than assumed — see the Dockerfile header.

> **The build is verified in CI, not on this machine.** The development sandbox
> has no Docker or Podman binary and no daemon, so `docker build` was never run
> here. GitHub Actions runs it on every push to `master`: build the image, start
> it, hit `/api/health` and let the `HEALTHCHECK` decide — and it passes (run
> 39917866288, 2026-10-23). What that does not prove: a registry push, a real
> deploy, or behaviour under load. The dependency audit above is static analysis
> of the real wheels, and the app is verified to run under the image's exact
> `CMD` locally.

## What mode a deployed instance runs

**No instance is deployed yet.** Everything needed to deploy in one step is
committed:

- `Dockerfile` — audited, no `apt-get`, runs as a non-root user, built and
  smoke-tested in CI.
- `render.yaml` — Render Blueprint, free tier, **live mode with a spend cap**.

**The committed choice is live mode (`DEMO_MODE=false`)**, because a visitor's
own upload is the point of the tool. Live is only safe with a ceiling, so the
blueprint sets five guard rails, all enforced by the app rather than by Render:

| Variable | Value | What it does |
|---|---|---|
| `USD_DAILY_CAP` | `2.00` | Hard ceiling per UTC day, computed from the ledger at `RUNS_DIR/spend_log.json`. At the cap, `POST /api/shipments` returns **429** naming the cap and the reset time; every other route keeps working. ≈1,000 documents at the measured per-document cost. `0` disables it. |
| `MAX_DOCUMENTS` | `3` | A fourth file is refused with a 400 that names the limit and the count that arrived. |
| `MAX_PAGES_PER_DOCUMENT` | `4` | Longer documents are refused rather than truncated — see `REJECT_OVERSIZE_PAGES`. |
| `REJECT_OVERSIZE_PAGES` | `true` | Refuse instead of silently reading the first N pages. Off locally by default, because truncation is what a laptop has always done; with it off the report discloses how many pages were skipped. |
| `RUN_TTL_HOURS` | `24` | Uploaded runs older than a day are deleted at startup. |

**The sample shipments are exempt by construction.** `/api/fixture/{name}` runs on
a reconciler whose settings force demo mode, whatever the deployment says, so the
three bundled examples replay captured model output and make **zero** API calls —
including after the cap is spent. Every report carries `run.sample: true`, and the
UI, the HTML export and the PDF all say "sample: cached model output, replayed
from cache".

**Health stays open and cheap.** `GET /api/health` contacts neither provider and
writes nothing, so it answers 200 in live mode with no key configured — which is
exactly the state a freshly created Render service is in before the secrets are
pasted in. `run_ttl_hours` is reported there so the page can state the retention
it actually applies rather than a hand-typed promise.

**The free tier has no persistent disk, and that has a real consequence:** runs
*and the spend ledger* live under `/tmp`, so a redeploy or restart resets the day
and can therefore reset the cap. The app says this on the page ("deleted after
24 h … lost when this instance redeploys") rather than only here. A paid disk
pointed at `RUNS_DIR` makes both survive restarts.

`render.yaml` documents the fallback if Render rejects `runtime: docker` on the
free plan (switch to the native Python runtime — no code change).

### Deploy it

```bash
# 1. this repo is already public on GitHub
# 2. Render dashboard -> New -> Blueprint -> select the repo
#    Render reads render.yaml automatically.
# 3. open the issued https://<name>.onrender.com URL, click "Conflicts"
```

Before recording or sharing, **cold-start the URL** (free instances sleep after
15 min idle and take ~60 s to wake) and confirm `/api/health` returns 200.

---
