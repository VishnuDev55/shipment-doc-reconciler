# Shipment Doc Reconciler

Cross-checks a shipment's paperwork. Upload 2–5 trade documents (commercial
invoice, packing list, bill of lading) as scans or phone photos, and the app
extracts six shipment-level fields from each, normalizes them deterministically,
and flags where the documents disagree — with the source line quoted as evidence.

**Engineering audit:** [`docs/AUDIT.md`](docs/AUDIT.md) records what was verified,
what is broken, and what is deferred, with `file:line` evidence for every claim.
Read it before trusting anything below the quick start.

## How this meets the two hackathon requirements

Built for the **Nebius × NVIDIA Global AI Hackathon**. Both requirements are met,
via **two separate providers** — read this before judging, because the split is
not obvious from a glance at the code.

| Requirement | How it is met |
|---|---|
| **"Runs on Nebius Token Factory"** | The **executive summary** feature makes a runtime call to Nebius Token Factory's inference API (`https://api.tokenfactory.nebius.com/v1/chat/completions`) using **`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`**, a Nemotron model hosted by Nebius. One call per shipment, verified live. |
| **"Uses at least one NVIDIA open-source model"** | **Document extraction** — the core function — uses **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`** via NVIDIA's own `build.nvidia.com` API. The summary model above is *also* an NVIDIA open model, just hosted by Nebius. |

**Be explicit about the division of labour:** the *primary* functionality
(vision extraction of all six fields) runs on **build.nvidia.com**. The Nebius
Token Factory role is the **reasoning/summary** call plus the *option* of vision
fallback. If you want extraction itself on Nebius, set `VISION_PROVIDER=nebius`
— that path is also live-verified, using `openbmb/MiniCPM-V-4_5`.

### Why two providers rather than one

The brief specifies `nvidia/Nemotron-Nano-V2-12b-VL` on Token Factory. Queried
live against the Nebius key provided, **that model is not available**:

```
GET  /v1/models                        -> 200, 24 models, none a Nemotron vision model
POST /v1/chat/completions
     model=nvidia/Nemotron-Nano-12B-V2-VL
                                      -> 404 "The model ... does not exist."
```

All four `nvidia/*` models on that account report `"modality": "text->text"`, and
sending an image to one returns `400 "This model does not support image input"`.
Checked against both reachable hostnames.

Rather than ship a vision pipeline that cannot run, extraction was moved to
NVIDIA's own hosted API, where a Nemotron vision model *is* reachable — and the
Nebius requirement is satisfied by a genuine runtime inference call. Both
shortcomings are documented here rather than papered over.

> **The brief's exact model is still unavailable.** `nvidia/nemotron-nano-12b-v2-vl`
> exists on build.nvidia.com but returns **HTTP 410 Gone**: *"has reached its end
> of life on 2026-08-26"*. The NVIDIA docs page for it is stale — it was still
> being edited two months ago and does not mention the retirement. The omni model
> used instead is the nearest currently-available NVIDIA vision model.

```
Nebius Token Factory   https://api.tokenfactory.nebius.com/v1/   -> summary (Nemotron text)
NVIDIA build.nvidia.com https://integrate.api.nvidia.com/v1/     -> vision (Nemotron omni)
```

## The idea in one table

A real shipment is documented three times by three different people, and they
disagree. Customs, the freight forwarder, and the buyer each want a different
number to be true. The interesting part is not reading the documents — it is
deciding whether a difference is an **error** or just a **different way of
writing the same thing**.

| Invoice says | Bill of lading says | Naive diff | This app says |
|---|---|---|---|
| `12,450.00 KGS` | `12.450,00 kg` | conflict | ✓ same — European decimal comma |
| `12,450.00 KGS` | `27,447.00 LBS` | conflict | ✓ same mass — 0.25 kg rounding |
| `1,200 pieces` | `100 cartons` | conflict | ⚠ different package basis, not an error |
| `8517.62.00` | `8517.62.0090` | conflict | ✓ same — HS is harmonized to 6 digits |
| `Nordwind … GmbH` | `Nordwind Industrial Supply` | conflict | ✓ same — legal suffix stripped |
| `MSKU3067322` | `MSKU3067328` | conflict | ❌ conflict, **and it says which one is wrong** |

That last row is the centrepiece. A container number carries an ISO 6346 mod-11
check digit, so the field **validates itself**. When two documents disagree and
exactly one number fails its own checksum, no ground truth and no second model
call is needed to say which document is probably wrong:

> The ISO 6346 check digit ARBITRATES: `packing_list.png` prints check digit 8
> where the algorithm computes 2, so that value is internally inconsistent.
> `MSKU3067322` (`bol.png`) is the more likely correct container.

---

## Architecture

Single Python process. The filesystem is the database. No frontend build step.

```
browser  index.html + app.js + app.css (hand-rolled tokens)   no build step
   |
   |  upload 2-5 docs     SSE progress (1s poll as fallback)
   v
+--------------------------------------------------------------------------+
|  FastAPI  app/main.py                                                    |
|    POST /api/shipments              GET  /api/shipments/{id}/report      |
|    POST /api/shipments/{id}/retry   GET  /api/shipments/{id}/export (zip)|
|    GET  /api/config                 POST /api/config   (GUI Configure)   |
|    GET  /api/health                 GET  /api/fixtures                   |
|    GET  /api/shipments              GET  /page/{id}/{path}  (evidence)   |
|    GET  /api/shipments/{id}/raw/{n} (transparency panel body)             |
|    GET  /api/shipments/{id}/events  (SSE progress; poll is the fallback)  |
|    GET  /api/fixture/{name}         GET  /                (static UI)    |
+--------------------------------------------------------------------------+
   |
   v
+--------------------------------------------------------------------------+
|  Reconciler  app/orchestrator.py                                         |
|    asyncio.gather over documents; Semaphore(4) inside the client         |
|    one failed doc never aborts the shipment                              |
+--------------------------------------------------------------------------+
   |
   +--> ingest.py        EXIF transpose, resize to 1600px, RGB, JPEG q85,
   |                     pypdfium2 for PDFs. No poppler, no system deps.
   |
   +--> client.py        ChatClient base: transport, capability probe,
   |                     backoff. Two endpoints, one per role:
   |
   |    VISION   ExtractionClient -> providers.vision_endpoint()
   |              NVIDIA build.nvidia.com  nvidia/nemotron-3-nano-omni-...
   |              (fallback: VISION_PROVIDER=nebius -> MiniCPM-V on Token Factory)
   |                |
   |                +--> cache.py  sha256(prompt_ver + PROVIDER + model + pages)
   |                |     hit => no network call at all (this is DEMO_MODE)
   |                v
   |              https://integrate.api.nvidia.com/v1/
   |
   |    SUMMARY  SummaryClient -> providers.summary_endpoint()
   |              ALWAYS Nebius: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
   |              one call per shipment, optional, can never fail a run
   |                v
   |              https://api.tokenfactory.nebius.com/v1/
   |              (cached by narrative_cache.py, keyed on the verdict signature)
   |
   +--> normalize/      pure Python, zero model calls
   |                     number weight quantity container hs_code party money
   |                     emits canonical (UI) AND compare_key (engine) together
   |
   +--> compare/        pure Python. tolerance table, cardinality gate,
                        ISO 6346 arbitration, outlier attribution, reasons
   v
+--------------------------------------------------------------------------+
|  ./runs/{shipment_id}/        the filesystem IS the database             |
|    report.json  <- the single polled artifact                            |
|    pages/  raw/  uploads/     <- zips straight to the export artifact    |
+--------------------------------------------------------------------------+
```

**Pydantic v2 is the source of truth.** The same models define request
validation, the JSON schema sent to the model, response parsing, and report
serialization.

**`canonical` vs `compare_key`.** Every normalized value carries both. The
comparison engine touches *only* `compare_key`; the UI touches *only*
`canonical` and `raw_value`. They are produced together and never derived from
each other. That separation is what stops the classic bug where a display
string like `"12,450 kg"` gets string-compared against `"12450.0 kg"`.

**The model never does arithmetic.** `value` is always a verbatim string. Unit
conversion, number parsing, checksum validation and currency handling all happen
in Python, so the audit trail survives and a hallucinated calculation is
structurally impossible.

**`fields` is an object keyed by the six fixed names**, not an array — so
structural completeness does not depend on the model remembering to include
everything.

---

## Live facts, measured not assumed

### Nebius Token Factory (`GET /v1/models?verbose=true`)

Pricing, context length and per-model limits are not in the docs at all — only
observable with a key. The 24 models returned include four `nvidia/*`, **all
`text->text`**:

| Model | Modality | Context | Prompt / 1M | Completion / 1M |
|---|---|---|---|---|
| `NVIDIA-Nemotron-3-Nano-30B-A3B` | text→text | 262,144 | $0.06 | $0.24 |
| `Nemotron-3_5-Lightning` | text→text | 1,048,576 | $0.06 | $0.24 |
| `nemotron-3-super-120b-a12b` | text→text | 262,144 | $0.30 | $0.90 |
| `Nemotron-3-Ultra-550b-a55b` | text→text | 1,048,576 | $1.00 | $3.00 |
| `openbmb/MiniCPM-V-4_5` (vision fallback) | text+image→text | 32,000 | $0.658 | $1.11 |

The summary model is the cheapest Nemotron that produced clean prose. Rate limit
observed live: **300 RPM / 200,000 TPM** for MiniCPM-V.

### NVIDIA build.nvidia.com

`GET /v1/models` returns 82 models. **No rate-limit, quota or credit headers are
returned at all** — spend visibility is dashboard-only, so live calls were kept
minimal. Measured behaviour:

| Observation | Value |
|---|---|
| Structured output | `json_schema` accepted (wrapped envelope) |
| Vision latency | 30–50 s per page (reasoning model) |
| Availability | intermittent `503 ResourceExhausted: Worker limit (16/16)` |
| Reasoning | returned in `reasoning_content` alongside `content` |

That 30–50 s latency is the honest trade-off of using a reasoning VLM: worth it
for document intelligence, but it means the demo should pre-warm or use the cache.

Measured on a real 1-page extraction: **1,926 prompt + 404 completion tokens =
$0.0017 per document**, so a 3-document shipment runs about half a cent. The
endpoint also reported 1,920 of those prompt tokens as cached.

## Quick start

```bash
make setup          # pip install -r requirements.txt
make dev            # http://localhost:8000 — DEMO_MODE, replays the captured cache
```

For live mode locally, either put the keys in `.env` and `make run`, or start
with `make dev` and paste them into **Configure** in the UI — see
[Configuring keys from the UI](#configuring-keys-from-the-ui).

Then click **Clean**, **Conflicts** or **Messy formats** — those are real document
images pushed through exactly the pipeline an upload uses, so live mode will spend
tokens on them.

To run it with no key at all, start with `DEMO_MODE=true`. The repo ships a response
cache holding **real captured model output** for all nine fixture documents, so that
mode replays genuine extractions and genuine AI summaries rather than hand-written
placeholders. It is also what the hosted instance runs, so a reviewer without keys
still sees model output — labelled as such on screen.

```bash
make test           # 391 tests, ~22s, zero API calls (socket guard, see Tests)
make probe          # rehearse the demo: prints the verdict table
make dist           # clean source zip in dist/
make docker         # single-stage image
```

### Running against the live APIs

You need **two keys from two different places**:

| Variable | Where to get it | Used for |
|---|---|---|
| `NVIDIA_API_KEY` | <https://build.nvidia.com/settings/api-keys> | document extraction (vision) |
| `NEBIUS_API_KEY` | <https://tokenfactory.nebius.com/project/api-keys> | the executive summary |

`cp .env.example .env`, then edit it to look like this:

```dotenv
# --- NVIDIA build.nvidia.com : vision extraction ---
NVIDIA_API_KEY=nvapi-...your-key...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1/
NVIDIA_VISION_MODEL=                 # blank = nvidia/nemotron-3-nano-omni-30b-a3b-reasoning

# --- Nebius Token Factory : executive summary ---
NEBIUS_API_KEY=...your-key...
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
NEBIUS_SUMMARY_MODEL=                # blank = nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B

# --- routing ---
VISION_PROVIDER=nvidia               # nvidia (default) | nebius (MiniCPM-V fallback)
DEMO_MODE=false                      # false = make real calls
ENABLE_SUMMARY=true
```

`.env` is gitignored and excluded from every archive; only `.env.example` is
committed, and it contains placeholders only.

Then verify both providers before trusting anything:

```bash
make probe-live     # probes each endpoint, one real extraction, one real summary
make probe-diff     # live extractions diffed against the authored cache
```

`scripts/probe_live.py` is the only thing that touches the network during
verification. It runs the capability probe on each endpoint, performs one real
vision extraction, and generates one real summary from a fixture report —
printing the actual response, usage and any error body.

### Configuring keys from the UI

**Configure** in the header opens a drawer that applies settings to the running
process — vision provider, `NVIDIA_API_KEY`, `NEBIUS_API_KEY`, the summary
toggle, and per-role model overrides — without a restart or a redeploy.

How it behaves, and what it costs:

- **Server memory only.** A key pasted into the drawer is never written to disk
  and never leaves the process. It is gone on restart, and `.env` still wins at
  boot.
- **Keys are write-only over HTTP.** `GET /api/config` returns
  `nvapi…1234`-style masks (first 5, last 4); `POST /api/config` treats an
  echoed mask as "unchanged" rather than storing it, and a 400 response reports
  only that a value was rejected, never what it was.
- **Applying rebuilds the clients.** `Reconciler.reconfigure` replaces the
  extraction and summary clients, because a `ChatClient` fixes its endpoint,
  structured-output mode and probe state at construction. Leaving demo mode
  without that rebuild would keep replaying the cache while claiming to be live.
- **Configuration is process-global, not per-user.** There is no session and no
  auth, so the last person to save a key sets it for everyone sharing that
  instance. Set `GUI_CONFIG_ENABLED=false` on anything public; `render.yaml`
  does exactly that, which is why the hosted demo is env-driven.

With the flag off, `GET /api/config` still reports status but `POST` returns 403
and the drawer explains where to set keys instead.

### Transparency panel

Every result screen ends with a **Transparency** table: one row per document plus
one for the summary, naming the provider, the model, whether the answer came from
a live call or the cache, attempts, tokens and latency. Below it, **Raw model
output** lists each persisted body from `runs/{id}/raw/` and shows it verbatim.

The panel is built to be unflattering where the facts are. A run that made no
calls says so in an amber caveat rather than showing cached numbers silently, a
replayed document reports zero tokens, and a hand-authored fixture is never
labelled as model output. Cost appears only if you set `USD_PER_MTOK`; with it
unset the panel prints token counts and declines to invent a price.

`GET /api/shipments/{id}/raw/{name}` serves those bodies as plain text, and only
for filenames the report itself listed — a name the report never named is not
evidence about that run.

### Progress: pushed, with a poll behind it

`GET /api/shipments/{id}/events` streams the report each time it settles. Every
mutation already funnels through `RunStore.write`, so that one hook is the fan-out
point — no second write path to keep in sync. The stream carries the same
document an immediate poll would, including a replay on subscribe, so a client
that joins mid-run sees the full picture rather than waiting for the next change.

The browser keeps the 1-second poll running until the first stream event proves
the channel is alive, then drops it. If `EventSource` is unavailable, the route
is blocked, or the stream errors, the interval is still there and the UI behaves
exactly as it always did. A stalled or disconnected client cannot slow a run
down: each listener is a bounded queue that drops its oldest frame, and the
generator unsubscribes in a `finally`.

The progress line counts documents settled and seconds elapsed, because a
reasoning vision model takes 30–90 s per document and a bare "Extracting…" reads
as a hang.

### Two readers, when you can afford the doubt

Set `CONSENSUS=true` and every page is also read by the *other* provider — NVIDIA
if the run is on Nebius, and vice versa, or pin it with `CONSENSUS_PROVIDER`. The
two answers are compared in deterministic Python on `compare_key`, and a field
they disagree on is marked **needs review** in amber, showing both transcriptions.
A second row per document appears in the transparency panel naming the second
model.

What it deliberately does not do:

- **It does not tie-break.** Two readers agreeing is not proof, and two
  disagreeing is not a verdict. The primary extraction is still what the report is
  built from; `tests/test_consensus.py` snapshots every comparison with and
  without consensus and asserts they are identical.
- **It does not compare spellings.** "NORDWIND IMPORT GmbH" and "Nordwind Import
  GmbH & Co" agree, because comparing on `compare_key` rather than `canonical`
  keeps the normalizer authoritative.
- **It keeps coverage separate from correctness.** A field one reader left blank
  is a coverage disagreement, not a transcription error, and is flagged as such.

Off by default because it roughly doubles token spend. The shipped cache holds a
captured NVIDIA response *and* a captured Nebius response for all nine fixture
documents, so consensus can be demonstrated offline against genuine transcriptions
from both hosts rather than invented ones.

### Model discovery

Model names are treated as defaults, not facts. Before a run extracts, the app
issues `GET /v1/models` on each host (with `?verbose=true` on Nebius, which is
where the modality comes from) and checks that the configured model is actually
served. If it is not, the app adopts one that is — preferring the Nemotron
family, then any other model that takes image input — and says so in the
transparency panel rather than failing every document silently.

This exists because the assumption already cost the project: the brief specified
`nvidia/Nemotron-Nano-12B-V2-VL`, which returned **410 Gone** on
build.nvidia.com and **404** on Token Factory, and that was found by hand, with a
live key, mid-build.

Rules worth knowing:

| Situation | What happens |
|---|---|
| Configured model is listed by the host | Kept. The client is not rebuilt, so no extra probe call. |
| Configured model is gone | A served model is adopted; `model_selection: "discovered"` and the note land in the report. |
| No key, host unreachable, odd payload | Nothing changes. `model_selection: "unverified"` with the reason. Discovery is advisory and must never abort a shipment. |
| `VISION_MODEL_ALLOW` / `SUMMARY_MODEL_ALLOW` set | Those names win over both the default and discovery. |
| `DEMO_MODE=true` | No discovery call at all. |

Ranking is sorted by preference band then model id, so two runs against the same
catalog pick the same model. `GET /api/models` reports the current decision and
`POST /api/models/refresh` re-queries past the TTL.

**Unverified live.** No API key was available while this was written, so the
discovery *logic* is tested against catalog payloads replayed through a mocked
transport (`tests/test_discovery.py`) — including both envelope shapes. What
neither host actually lists today has not been checked. Run `make probe-live` to
confirm before trusting the outcome.

### The adjudicator: a second opinion, never a second decision

Two of the six fields are genuinely interpretive — whether "ACME Freight Ltd" and
"Acme Freight Limited" are the same party, and whether "12 pallets" and "144
cartons" describe the same shipment. Everything else is arithmetic or a check
digit. So on exactly those two cases, and only when the deterministic engine
already said `suspicious`, the app asks a Nebius Nemotron model what it thinks.

**The verdict does not move.** The opinion is stored beside the comparison as
`adjudication` and rendered under the engine's own reason, labelled
"advisory, not applied". A model that confidently disagrees with a tolerance band
or an ISO 6346 result changes nothing — there is no code path that lets it.
`tests/test_adjudicate.py` asserts this by snapshotting every decided field
before and after.

Also added:

- **A customs-risk strip**, computed by rule from the verdicts already produced.
  It is explicitly *not* a model output and says so on screen: `source:
  "deterministic"`. A disagreement in HS code or container number is `high`;
  weight and unreadable documents are `medium`.
- **A draft discrepancy email**, written from the verdicts, guarded against
  scaffolding and chain-of-thought, cached per verdict set, and labelled a draft
  that must be checked before sending.

Every part of this is optional and failure-safe. Disabled by flag, skipped in
`DEMO_MODE` (an authored "opinion" replayed as model output would be a fabricated
result, so none are shipped), skipped without a key, and a transport failure
records `failed` with the tokens it still spent rather than sinking the run.

### Optional: run vision on Nebius instead

```bash
VISION_PROVIDER=nebius   # extraction uses openbmb/MiniCPM-V-4_5 on Token Factory
```

`MiniCPM-V-4_5` is the only `text+image→text` model on the Nebius key used during
development. It is a **documented fallback, not the default** — the default is the
NVIDIA Nemotron omni model, so that extraction runs on an NVIDIA open model.
Measured: MiniCPM-V is ~10× faster (2.7–4.0 s vs 34–88 s per page) but on these
fixtures was *less* accurate (it invented a digit; the omni model transcribed
correctly). Both paths are live-verified on all 9 fixture documents.

### With Docker

```bash
docker build -t shipment-doc-reconciler .
docker run -p 8000:8000 shipment-doc-reconciler            # live; paste keys into Configure
docker run -p 8000:8000 -e NVIDIA_API_KEY=... -e NEBIUS_API_KEY=... \
  shipment-doc-reconciler                                  # live, keys from the environment
docker run -p 8000:8000 -e DEMO_MODE=true shipment-doc-reconciler   # keyless cache demo
```

The image needs **no `apt-get`**: all 69 transitive dependencies resolve to
binary manylinux wheels, and every shared-library dependency is either vendored
inside a wheel or on the `manylinux_2_28` whitelist. This was audited with
`readelf`/`auditwheel` rather than assumed — see the Dockerfile header.

> **`docker build` has not been executed.** The development sandbox has no
> Docker or Podman binary and no daemon. The dependency audit above is static
> analysis of the real wheels, and the app is verified to run under the
> image's exact `CMD` locally, but the build itself is unconfirmed.

---

## Demo / deployment status

**No public hosted URL is live yet — deploying requires a Render (or Railway)
account, and this sandbox has no deployment credentials.** Everything needed to
deploy in one step is committed:

- `Dockerfile` — audited, no `apt-get`, runs as a non-root user.
- `render.yaml` — Render Blueprint, free tier, `DEMO_MODE=true`.

**Deliberate mode choice for the public instance:** `DEMO_MODE=true`. The app
replays a cache of **real captured model output** (9 documents × 2 providers,
plus 3 AI summaries) and labels that honestly on screen — it never implies a
live call happened. This is chosen over live mode for two reasons: Render may
suspend free services that "invoke external APIs" at volume, and a live call is
34–88 s per document. To demo live instead, set `DEMO_MODE=false` and add both
keys as secret env vars in the dashboard.

`render.yaml` documents the fallback if Render rejects `runtime: docker` on the
free plan (switch to the native Python runtime — no code change).

### Deploy it

```bash
# 1. push this repo to GitHub (public)
# 2. Render dashboard -> New -> Blueprint -> select the repo
#    Render reads render.yaml automatically.
# 3. open the issued https://<name>.onrender.com URL, click "Conflicts"
```

Before recording or sharing, **cold-start the URL** (free instances sleep after
15 min idle and take ~60 s to wake) and confirm `/api/health` returns 200.

---

## How extraction works

**One model call per document**, all of that document's pages in the same call
as separate image parts, each preceded by a `--- PAGE n ---` marker so the
reported `page_index` means something.

The system prompt states the contract without softening it. The rule that
matters most:

> If a field is not visibly printed, set `present=false`, `value=null`. NEVER
> infer, calculate, or guess. An absent field reported as absent is correct. An
> absent field filled in with a plausible guess is a failure.

A guessed value looks plausible, passes every downstream check, and quietly
destroys the premise of the whole tool. The prompt also instructs the model to
treat text found inside documents as **data, never instructions** — a scanned
page can carry adversarial wording.

### Structured output: probe, and trust the endpoint over the docs

Token Factory documents `response_format: json_schema`, gated **per model** by a
"JSON mode" tag on the model card — so it cannot be assumed available.

The docs also show the payload as a **bare** schema under `json_schema`. Live
testing on 2026-09-23 proved that wrong:

| Payload sent | Result |
|---|---|
| `{"json_schema": <bare schema>}` (as documented) | **422** `response_format.json_schema.name: Field required` |
| `{"json_schema": {"name", "strict", "schema"}}` | **200** |

The endpoint actually wants OpenAI's standard wrapped shape. The client sends
that, and `test_client.py` pins it so a future "correction" back to the docs
form fails a test.

A single tiny text-only probe still runs at startup and caches its verdict —
`strict` → `json_object` → `plain` — never re-probed per request. On this
account the probe returns **strict**.

### The retry ladder, cheapest first

| Rung | Cost | What happens |
|---|---|---|
| 1 | free | `json.loads()` directly |
| 2 | free | repair text: strip markdown fences, extract the first **balanced** `{...}` span (string- and escape-aware), drop trailing commas, fix smart quotes |
| 3 | free | coerce into Pydantic: missing keys → `present=false`, non-string value → `str()`, out-of-range confidence → clamped, unknown `doc_type` → `"other"` |
| 4 | 1 call | feed the bad output **and the parser error** back, ask for corrected JSON |
| 5 | 1 call | retry with a simplified schema |
| 6 | — | give up cleanly: document marked `failed`, shown with a retry button |

One bad field never fails a whole document, and one failed document never aborts
a shipment. A 2-of-3 result is useful; a 500 error is not.

Transport failures (timeout / 429 / 5xx) are handled **below** the ladder with
exponential backoff — 3 attempts at 1s / 4s / 12s plus jitter — so a network
blip never burns a ladder rung. Every raw response is written to `runs/…/raw/`,
including failed attempts.

---

## Normalization and comparison

Dispatched by field kind. Pure functions, no I/O, no model calls.

**Numbers** — the highest-value rule, because trade documents are genuinely
international. Both separators present → the rightmost is decimal, so
`12.450,00` and `12,450.00` both parse to 12450.00. Space and apostrophe
grouping are stripped and the value re-parsed. A lone comma before exactly three
digits is **ambiguous** — `1,200` could be a thousands separator or a European
decimal. It defaults to the thousands convention and **emits a warning**, because
guessing silently is worse than guessing and admitting it.

OCR digit repair (`O→0`, `l→1`, `S→5`, `B→8`, `Z→2`) applies only as a last
resort, only when a token fails to parse **and** exactly one substitution makes
it parse, and is always flagged. Never applied to a token that already parses.

**Weight** → canonical kg. `kg/kgs/kilogram ×1`, `g ×0.001`, `mt/tonne ×1000`,
`lb/lbs/pound ×0.45359237`. A bare `ton` or `t` is genuinely ambiguous between
metric and short ton — defaults to metric and warns.

**Quantity** → count **and** package kind extracted separately. `piece/set` are
an *item* basis; `carton/pallet/box/case` are a *packaging* basis. Different
bases with different counts is ⚠ "likely different unit basis", not ❌ — this is
the single most common legitimate discrepancy in real paperwork. Two different
packaging types with different counts *is* a conflict.

**Container number** → full ISO 6346. Strip to uppercase alphanumeric, match
`^[A-Z]{4}[0-9]{7}$`, then verify the mod-11 check digit (letter values A=10,
B=12… skipping multiples of 11; weighted by powers of 2; `sum mod 11 mod 10`).
Verified against the standard's own published worked example: `CSQU305438` →
weighted sum 6185 → check digit **3**.

**HS code** → digits only, compared on the **first 6**. Digits 7–10 are national
subdivisions that legitimately differ between exporter and importer paperwork;
full-string comparison manufactures false conflicts.

**Consignee** → NFKD accent folding, uppercase, punctuation stripped except `&`
and `-`, legal suffixes removed repeatedly from the end, then compared with
rapidfuzz `token_set_ratio`. Thresholds are frozen in one place: 100 exact,
≥90 consistent (fuzzy), 70–89 suspicious, <70 conflict.

**Money** → detect ISO code or symbol, strip Incoterms and the spelled-out
"SAY … ONLY" line, parse with the number rules. **Currencies are never
converted.** Different currencies is always a hard conflict — correct behaviour,
and no FX dependency.

### Cardinality gate

Before any comparator runs: 0 documents have the field → `absent` (warned about
only if that field is normally expected for the uploaded document types);
exactly 1 → `single_source`, **never** rendered as a conflict since there is
nothing to reconcile against; 2+ → run the comparator. With 3+ documents,
matching values cluster and the minority is marked as an outlier — which is why
the demo uses three documents, not two.

Every result carries a **templated** reason string with the actual numbers
interpolated. Deterministic, free, auditable.

---

## Design decisions worth stating out loud

**No OCR fallback.** Layering Tesseract or PaddleOCR under the VLM would mean
adjudicating between two extractions that disagree — a multi-day rabbit hole for
no demo gain. The signal OCR would provide ("this value is suspect") is already
produced three ways: the ISO 6346 checksum catches misreads with certainty, the
model's self-reported confidence flags illegibility, and normalizer warnings
flag ambiguity. Each is cheaper and more honest than a second engine.

**No bounding boxes.** VLM box coordinates are unreliable and the UI around them
is fragile. Evidence here is a verbatim quoted line plus a page number plus the
viewable page image — enough to verify a value in about two seconds.

**Filesystem over a database.** There are no queries, no relations and no
multi-user access. `runs/{shipment_id}/` *is* the artifact — the ZIP export is
literally that directory compressed.

**Polling over WebSockets.** One JSON file per shipment, polled every second.

**No auth.** Shipment IDs are `secrets.token_urlsafe(12)`. Unguessable is
sufficient access control for a public demo.

**Six scalar fields, not line items.** Per-SKU matching is future work, and
line-item reconciliation would multiply the comparison surface without adding a
single new idea to the demo.

---

## Export

`Download reconciliation ZIP` produces:

```
report.json    full machine-readable report, with evidence and version tags
report.csv     one row per field per document — pivots cleanly in a spreadsheet
report.html    standalone, self-contained: inline CSS, evidence images embedded
               as data URIs. No CDN, no network. Opens straight out of the zip.
README.txt     what this is
pages/         the normalized page images used as evidence
raw/           every raw model response, including failed attempts
```

`report.json` records `prompt_version`, `normalizer_version`,
`comparator_version` and `model`, so a cached response can never be silently
reconciled against a newer, incompatible ruleset.

---

## Tests

```
391 passed in ~22s
```

~10 s is expected and is CPU work, not waiting: `test_pipeline`, `test_api` and
`test_client` deliberately re-run the **real** image/PDF ingestion path
(~128 ms per page, ~730 ms for the 2-page PDF) rather than stubbing it, so they
prove the pipeline end to end instead of proving a mock. No single test exceeds
2 s. If it ever gets much slower, suspect a network timeout, not the math.

### The offline guarantee is enforced, not documented

`tests/conftest.py` patches `socket.connect`, `connect_ex`,
`create_connection`, `getaddrinfo` and `gethostbyname` for the whole session.
Anything that is not loopback raises `AssertionError` naming the target. That
covers every provider host at once — `api.tokenfactory.nebius.com`,
`integrate.api.nvidia.com` and any future one — instead of relying on a list of
names to remember.

Loopback stays open because `TestClient` reaches the app through httpx's
in-process `ASGITransport`, which never opens a socket;
`test_offline_guard.py` pins both halves of that, including a test that builds a
**live-mode** `ChatClient` with a key that passes the placeholder check and
asserts the call dies on the wall rather than reaching the provider.

Before this existed the guarantee was convention only: tests stayed offline
because each one passed `demo_mode=True` or injected a fake client, so a single
new test in the wrong shape could have spent real credits in CI.

| File | Covers |
|---|---|
| `test_normalizers.py` | number/weight/quantity/container/HS/party/money tables |
| `test_compare.py` | tolerance bands, cardinality gate, arbitration, outliers |
| `test_parse.py` | retry-ladder rungs 1–3, "one bad field ≠ dead document" |
| `test_client.py` | payload shape, probe, ladder call counts, backoff, cache |
| `test_pipeline.py` | all three fixtures end to end through DEMO_MODE |
| `test_api.py` | real HTTP against the FastAPI app, ZIP contents, traversal |
| `test_config.py` | runtime Configure: masking, client rebuild, 400/403 refusals |
| `test_live_fixtures.py` | captured **live** responses; transcription and confidence quirks pinned |
| `test_providers.py` | two-provider routing, cache isolation, reasoning fields, summary safety |
| `test_offline_guard.py` | the socket guard itself: remote refused, loopback allowed |

Zero API calls in the whole suite. `test_client.py` and `test_providers.py` use
fake transports, so they pin the exact `response_format` payload each provider
receives without a key.

---

## Verification status

**Both providers have been called live and both work.** What is verified, and
what is not:

The live rows below rest on the captured responses and raw bodies committed
under `fixtures/live_responses/`, which is why they can be checked without a
key. They were **not re-run during the October 2026 audit**, because no API key
was available; nothing on this list has been re-verified against a live endpoint
since. Model availability on both hosts changes without notice —
`nvidia/nemotron-nano-12b-v2-vl` is on this list precisely because it went 410
Gone mid-project — so treat every ✅ as "true as of the capture, re-run
`make probe-live` to confirm".

| | Status |
|---|---|
| Normalizers, comparators, parse ladder | ✅ 391 unit tests |
| End-to-end pipeline, UI, ZIP export, HTTP API | ✅ cache-verified **and** live-verified |
| Vision extraction, NVIDIA build.nvidia.com | ✅ 9/9 fixture documents; 11 calls, every one parsed on ladder rung 1 |
| Vision extraction, Nebius MiniCPM-V (fallback) | ✅ 9/9 documents, all parsed on attempt 1 |
| Nebius summary call (the hackathon requirement) | ✅ live, one call per shipment |
| `response_format: json_schema` (strict) | ✅ accepted on **both** hosts — wrapped shape only |
| Base64 data URIs | ✅ accepted on both |
| Checksum arbitration on live output | ✅ flagged the planted bad container number |
| Cross-page evidence (`page_index`) | ✅ live model reported page 2 correctly |
| Extraction determinism | ✅ two independent calls on one page were byte-identical |
| Pricing / context / rate limits | ✅ live `GET /v1/models?verbose=true` (Nebius) |
| `nvidia/nemotron-nano-12b-v2-vl` (brief's model) | ❌ **410 Gone on build.nvidia.com**, 404 on Nebius |
| `docker build` | ⚠️ **never run** — no Docker daemon in this sandbox |

### Live findings that changed the code

- **Both providers reject the `json_schema` shape their docs show.** Nebius:
  422 `json_schema.name: Field required`. NVIDIA: 400 "missing field name".
  Both accept OpenAI's wrapped `{name, strict, schema}`. Client fixed; tests pin
  the working envelope so a future "correction" back to the docs form fails.
- **The prompt's shape example was being copied verbatim.** Given a page headed
  `PACKING LIST`, the model returned `doc_type: commercial_invoice` with the
  example's reason word-for-word — it pattern-matched the example instead of
  reading the page. The example is now structurally complete but deliberately
  un-copyable, and a user's declared document type now overrides the model's
  guess (recorded as a disagreement, never silently applied).
- **A 503 was being read as "this mode is unsupported."** NVIDIA's shared
  workers return `ResourceExhausted: Worker limit (16/16)` under load; the probe
  treated that as a capability verdict and permanently downgraded a model that
  had accepted `strict` minutes earlier. Capacity is now retried, capability is
  not, and the probe's transport attempts are capped so it cannot spend 15 calls
  answering one yes/no question.
- **Reasoning models do not populate `content` reliably.** NVIDIA returns
  `content` *and* `reasoning_content`; Nebius Nano returned `content: null` with
  the answer in `reasoning`. Reading `.content` alone yields an empty extraction
  on HTTP 200. `_message_text()` handles both, and an empty completion is now
  retried rather than accepted.
- **The model invents digits.** MiniCPM-V returned `13,200.000 KGS` for a page
  printing `13,200.00 KGS`, and repeated the invention in its own
  `evidence_text`. Normalization absorbs it, so no verdict changes — but the UI
  still shows the model's raw string, so the mis-transcription stays visible.
- **Confidence is not load-bearing.** A model reported `confidence: 1.0` on the
  value it had just mis-transcribed. Confidence can only escalate a *consistent*
  field to suspicious; the ISO 6346 checksum and the tolerance table decide
  correctness.
- **Incoterms get dropped despite a verbatim instruction** (`FOB NINGBO
  USD 8,420.00` → `USD 8,420.00`). Both normalize to the same `compare_key`.
- **Two hand-authored fixture values were wrong and the model was right** — an
  all-caps consignee the template never renders, and a `page_index` that was
  correct only by luck because the totals were not actually on page 2. Both
  fixed against the document image, not "fixed" to match the model.
- **The AI summary did arithmetic and got it wrong.** Asked how many fields
  "need attention", it counted the two single-source fields on a shipment with
  zero problems. The prompt now supplies precomputed counts so the model only
  phrases them. Separately, the plausibility guard had to learn to reject
  **truncated** answers — a reasoning model that spends its token budget
  thinking can return `"...need attention. Gross"`, which passes every
  chain-of-thought check but is not a sentence.


## Layout

```
app/
  config.py            pydantic-settings; both keys; version tags
  providers.py         endpoint routing: vision vs summary, per provider
  fields.py            the frozen six-field set; per-doc-type expectations
  models.py            every Pydantic model + the hand-written JSON Schema
  prompt.py            system prompt, field spec, page markers, corrections
  client.py            ChatClient base; ExtractionClient + SummaryClient;
                       capability probe, ladder, per-provider backoff
  summary.py           executive-summary prompt + plausibility guard
  narrative_cache.py   summaries keyed on a verdict signature
  parse.py             ladder rungs 1-3 (free, local)
  cache.py             sha256(prompt_ver + PROVIDER + model + page hashes)
  ingest.py            EXIF/resize/JPEG; pypdfium2 for PDFs
  normalize/           number weight quantity container hs_code party money
  compare/             comparators, tolerance table, cardinality gate, reasons
  orchestrator.py      gather over documents; report assembly; summary step
  export.py            ZIP: report.json + report.csv + standalone report.html
  store.py             ./runs/{id}/ - filesystem as database, atomic writes
  main.py              FastAPI routes; /api/health reports both providers
web/                   index.html . app.js . app.css   (no build step)
fixtures/
  templates.py         the three documents as HTML
  shipments.py         one dict drives both the page and its authored response
  images/              9 documents (10 pages; one is a 2-page PDF)
  responses/           27 entries / 9 documents:
                         9 nvidia live . 9 nebius live . 9 hand-authored
  responses/narratives/  3 real AI summaries, keyed on verdict signature
  live_responses/      captured live responses + raw bodies (incl. a
                       duplicate call kept as a determinism datapoint)
scripts/
  build_fixtures.py    render -> degrade -> ingest -> author cache
  selfcheck.py         demo rehearsal, zero API calls
  probe_live.py        verify BOTH providers against the real APIs
  live_compare.py      live extractions diffed against the authored cache
  capture_summaries.py write real Nebius summaries into the demo cache
  migrate_cache_keys.py re-key the cache after a format change
tests/                 391 tests across 15 files (+ conftest.py socket guard)
docs/                  AUDIT.md — evidence-backed findings and the deferred list
```

The three-layer cache is deliberate. Each fixture document has a **NVIDIA** live
response, a **Nebius** live response, and a **hand-authored** fallback. Any
`VISION_PROVIDER` setting finds a real answer, and provenance is labelled
honestly in the report — replaying an authored fixture is never reported as a
model result.


## License

MIT.
