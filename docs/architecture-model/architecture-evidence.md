# Evidence index — runtime architecture diagram

Every node and edge in `architecture.png` traces to the file that makes it true.
Confidence is **evidenced** when a route, call or setting in the tree says so, and
**inferred** when the diagram groups or abstracts several of those. Nothing here is
from folder names alone.

Generated from commit `0be84c1`. Regenerate with
`.venv/Scripts/python.exe scripts/render_architecture.py`; the diagram's own
subtitle carries the commit it was drawn from, so a stale picture is visible
rather than silent.

## Nodes

| Node | Evidence | Confidence |
|---|---|---|
| Reviewer | A person using the browser UI; no actor model exists in code | inferred |
| UI shell | `web/index.html`, `web/app.js`, `web/app.css`; served at `/` and mounted at `/static` (`app/main.py:235`) | evidenced |
| Report transport | `web/app.js` polls `/api/shipments/{id}/report` at 1 Hz and falls back to `EventSource` on `/events`; `app/main.py:shipment_events` | evidenced |
| Routes | 22 route decorators in `app/main.py`; `/api/health` and `/api/session` are the two without `require_api_key` | evidenced |
| Auth | `app/auth.py` (`require_api_key`, `key_is_valid`, `secret_matches`, `_session_valid`); cookie issued by `open_session` | evidenced |
| Rate limiter | `app/ratelimit.py` (`_client_key`, `_limiter_for`), `app/limits.py` (`RateLimiter`) | evidenced |
| Spend gate | `app/budget.py` (`SpendGate`, `estimate_run_usd`, `reserve`), reached through `app/main.py:_gate_spending` | evidenced |
| `spend_log.json` | `app/budget.py:log_path` → `RUNS_DIR/spend_log.json`; written by `record_call` after the provider answers | evidenced |
| Config + models | `app/config_view.py` (`_mask`, `_require_config_token`), `/api/config` and `/api/models` routes | evidenced |
| Pipeline stages | `app/orchestrator.py` (`create_shipment`, `_process`, `refresh_report`); `ingest.py`, `verify.py` + `textlayer.py`, `compare/` + `normalize.py`, `rules.py` + `mathcheck.py`, `scoring.py`, `severity.py` | evidenced |
| consensus / line items | `app/consensus.py`, `app/lineitems.py`; both default **off** (`config.py:128`, `:136`) | evidenced |
| adjudication / summary | `app/adjudicate.py`, `app/summary.py`; both default **on** (`config.py:119`, `:66`) | evidenced |
| SummaryClient | `app/client.py:SummaryClient`, endpoint from `app/providers.py:summary_endpoint` | evidenced |
| Discovery + probe | `app/discovery.py`, `app/providers.py`, capability probe in `app/client.py` | evidenced |
| ExtractionClient | `app/client.py:ExtractionClient`; `VISION_FALLBACK` defaults true (`config.py:59`) | evidenced |
| `runs/<id>/` | `app/store.py` module docstring and `RunStore` (`report.json`, `meta.json`, `uploads/`, `pages/`, `raw/`) | evidenced |
| `fixtures/responses/` | `app/config.py:COMMITTED_CACHE_DIR`; read first by `app/cache.py:read_path` | evidenced |
| `.live_cache/` | `app/config.py:cache_write_dir` and `live_cache_ttl_hours`; pruned by `app/cache.py:purge_cache_older_than` | evidenced |
| NVIDIA / Nebius | `app/providers.py:NVIDIA_BASE_URL`, `NEBIUS_BASE_URL` | evidenced |
| One container, two configs | `Dockerfile`, `render.yaml`, `docker-compose.yml`, `.github/workflows/ci.yml` | evidenced |

## Edges

| Edge | Meaning | Evidence |
|---|---|---|
| transport → Routes | HTTPS, poll and SSE | `web/app.js`, `app/main.py` |
| Routes → Auth / limiter / gate | each request passes the guards before work | `_gate_spending`, `require_api_key`, `_limiter_for` |
| Routes → pipeline | upload or retry dispatches a background task | `orchestrator.create_shipment` → `asyncio.create_task(self._process(...))` |
| pipeline → ExtractionClient | one vision call per document, retried | `client.extract_document`, `_extract_locked` |
| pipeline → SummaryClient | narrative pass, dotted because it is optional | `_write_summary` |
| ExtractionClient → providers | HTTPS, retried with backoff | `client.transport_call`, `BACKOFF_SCHEDULE` |
| ExtractionClient → caches | get / put keyed on page sha256 + prompt version | `client._store_success` → `cache.put` |
| pipeline → `runs/<id>/` | `report.json` rewritten as each document settles | `orchestrator._process`, `store.write` |
| Routes → `runs/<id>/` | the API reads `report.json` and serves `pages/` and `raw/` | `get_report`, `serve_page`, `get_raw` |

The two left-hand rails carry the last two rows of that table; they are drawn as
edges rather than labels because both cross five bands.

## What the picture deliberately leaves out

- **Model ids, prices and caps.** They are configuration (`config.py`), change per
  deployment, and belong in `docs/PROVIDERS.md` rather than a structural diagram.
- **The retry ladder and its timing.** `BACKOFF_SCHEDULE` is an implementation
  detail of one edge (`ExtractionClient → providers`).
- **Deployment topology.** The container is one node here. Where it runs, behind
  what, and how it scales is a different view: `deployment-topology-analyzer`.
- **The verdict pipeline inside `compare`.** Ten fields, normalisation and the
  ISO 6346 arbitration are one node; `docs/ARCHITECTURE.md` explains the ordering.

## Why SVG rather than DOT

The Graphviz skill's default delivery is a DOT source plus a rendered export.
**Graphviz is not installed on this machine and there is no network to add it**, so
a `.dot` file would be an artifact nobody here can render, and it would drift from
the picture the moment the picture was edited. Instead the structure lives once in
`scripts/render_architecture.py`, which emits `architecture.svg` and rasterises it
with the Chromium build the e2e suite already requires. If Graphviz is available
later, the node/edge tables above transpose to DOT directly.
