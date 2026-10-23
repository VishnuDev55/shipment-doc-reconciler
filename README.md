# Shipment Doc Reconciler

<!-- CI badge, live: the workflow runs on push to master at
     github.com/VishnuDev55/shipment-doc-reconciler. It has executed for real --
     the first run on 2026-10-23 came back red on a dependency pin, which is the
     kind of thing a static "CI: passing" badge would have hidden. The result
     observed on GitHub is recorded in docs/DEPLOYMENT.md. -->
[![CI](https://github.com/VishnuDev55/shipment-doc-reconciler/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/VishnuDev55/shipment-doc-reconciler/actions/workflows/ci.yml)
![tests](https://img.shields.io/badge/tests-offline_%2B_browser-brightgreen)
![network in tests](https://img.shields.io/badge/test_network_calls-0_by_construction-blue)

**One line to know whether a shipment can move.** Drop 2–5 trade documents and
the app transcribes ten fields with a vision model, then reconciles them in
deterministic Python — the model never does arithmetic, converts currency, or
invents a value it cannot see. Every disagreement is quoted back to the line it
came from, every finding gets one severity, and the result is a self-contained
report (HTML + PDF + ZIP) plus correction drafts written by rule. **Measured
live 2026-10-19:** 70/70 fields exact, p50 59 s / p95 67 s per document,
≈$0.01 per shipment, and the second reader caught its own invention on every
divergence.

Cross-checks a shipment's paperwork. Upload 2–5 trade documents (commercial
invoice, packing list, bill of lading, certificate of origin) as scans or phone
photos, and the app extracts ten shipment-level fields from each, normalizes them
deterministically, and flags where the documents disagree — with the source line
quoted as evidence.

**Engineering audit:** [`docs/AUDIT.md`](docs/AUDIT.md) records what was verified,
what is broken, and what is deferred, with `file:line` evidence for every claim.
Read it before trusting anything below the quick start.

## Documentation

| Read | For |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | the shape of the system, the seven invariants and where each is enforced |
| [`docs/NORMALIZATION.md`](docs/NORMALIZATION.md) | every rule and threshold that decides whether two printings of a fact agree |
| [`docs/PROVIDERS.md`](docs/PROVIDERS.md) | what NVIDIA and Nebius actually do, measured by calling them |
| [`docs/EVAL.md`](docs/EVAL.md) | measured accuracy, generated from captured model output — and its limits |
| [`docs/PLANTED.md`](docs/PLANTED.md) | the planted-error bundle: what the engine catches, what it does not, and the one miss declared in advance |
| [`docs/SECURITY.md`](docs/SECURITY.md) | the threat model, including the four things not protected |
| [`docs/API.md`](docs/API.md) | the HTTP surface, auth contract, curl/Python/JS clients |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | deploy paths, health semantics, what is *not* verified here |
| [`docs/adr/`](docs/adr/) | nine decision records, including two reversed by measurement |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | setup, the gates, the rules that are not negotiable |
| [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) | ten minutes offline, plus the questions and the honest answers |

## How this meets the two hackathon requirements

Built for the **Nebius × NVIDIA Global AI Hackathon**. Both requirements are met,
via **two separate providers** — read this before judging, because the split is
not obvious from a glance at the code.

| Requirement | How it is met |
|---|---|
| **"Runs on Nebius Token Factory"** | Four runtime roles against `https://api.tokenfactory.nebius.com/v1/chat/completions`: the **executive summary** on **`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`** (one call per shipment), the optional **consensus second reader** and **advisor** on **`openbmb/MiniCPM-V-4_5`**, the **vision fallback**, and — with `VISION_PROVIDER=nebius` — **extraction itself**, measured on both `openbmb/MiniCPM-V-4_5` and `nvidia/Nemotron-3.5-Super-VL`. All four verified live; `docs/PROVIDERS.md` has the per-call evidence. |
| **"Uses at least one NVIDIA open-source model"** | **Document extraction** — the core function — uses **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`** via NVIDIA's own `build.nvidia.com` API. Every Nebius model named above is also an NVIDIA open model or an explicitly image-capable one; `Nemotron-3.5-Super-VL` is NVIDIA's, hosted by Nebius. |

**Measured, not remembered.** Three live days are in `docs/EVAL.md`, each dated:

- **2026-10-19, full pipeline** (extraction + MiniCPM-V consensus + goods lines +
  summary, real uploads): NVIDIA omni read **70/70** fields exact at p50 **59 s** /
  p95 **67 s** per document; MiniCPM-V consensus agreed **95.5%** and all five of
  its divergences were its own invention; ≈**$0.01** per shipment. The NVIDIA host
  was capacity-degraded that day (intermittent HTTP 503s and request hangs), so a
  document occasionally failed — and the verdict honestly weakened rather than
  guessing.
- **2026-10-21, `nvidia/Nemotron-3.5-Super-VL` as a candidate default**
  (`scripts/eval_model.py`, 16 documents × 2 runs): **16/16** parses,
  **204/206 (99.0%)** fields exact, **0** invented, p50 **4.5 s** / p95 **21.2 s**
  at **$0.0034** per document — about **13× faster** than omni's measured p50, and
  *more* expensive, since the omni estimate is ~$0.002 per document. The trade is
  speed for money, not the other way round.
- **2026-10-23, that model through the whole engine**: three demo shipments plus
  the 19-group planted bundle, consensus off and on, each twice — **3/3** verdicts
  every pass, **0** invented fields every pass, **74/75 (98.7%)** exact on the demo
  documents with the same single miss reproduced four times out of four. In the
  real pipeline its p50 is **10.0 s** / p95 **22.1 s**, slower than the isolated
  eval because each call now carries the engine's full prompt.

**The promotion rule and why it did not fire.** Super-VL was tested against a rule
written before the run: default vision model if it reaches 3/3 verdicts, 0 invented
fields and exact ≥ 99% on the demo documents. It cleared the first two and missed
the third — 98.7% against omni's 100% on the same 75 observations. So **omni stays
the default** and Super-VL is selectable (`NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL`);
no "Nebius-only" preset was added, because the measured evidence does not support
one. See `fixtures/model_comparison.json` — the numbers there are data, not prose.

**Be explicit about the division of labour:** the *primary* functionality (vision
extraction of all ten fields) runs on **build.nvidia.com**. The Nebius Token Factory
role is the **reasoning/summary** call, the consensus and advisor readers, the vision
*fallback*, and a selectable vision *primary*. If you want extraction itself on
Nebius, set `VISION_PROVIDER=nebius` — that path is live-verified on both models. On
`openbmb/MiniCPM-V-4_5`, which Token Factory declares `text+image→text`, it is
measurably weaker than the default: **75/75** exact on the comparison key but
**4 invented** field values and **2/3** verdicts where NVIDIA shows 0 invented and
3/3, all of them visible in the report. See `SUBMISSION.md`.

Two caveats on the harsher input cases, because they are the ones people quote:
the German and French stress pages are **synthetic documents this repo generates**
(`scripts/build_stress_inputs.py`), not translations of real paperwork — that is
what makes their exact strings scorable. And the **10/10** German and French
readings and the phone-photo before/after are measured on **Nebius MiniCPM-V
only**: NVIDIA omni hung past the 150 s deadline on the German page and on both
photo variants, so **no omni number exists for those three cases**. "Not measured"
is the honest wording, and it is not evidence of weakness either way.

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
|    GET  /api/shipments/{id}/report.pdf      GET  /api/shipments/{id}/events|
|    GET  /api/config                 POST /api/config   (GUI Configure)   |
|    GET  /api/health                 GET  /api/fixtures                   |
|    GET  /api/shipments              GET  /page/{id}/{path}  (evidence)   |
|    GET  /api/shipments/{id}/raw/{n} (transparency panel body)             |
|    GET  /api/shipments/{id}/events  (SSE progress; poll is the fallback)  |
|    POST /api/shipments/{id}/documents/{doc}/fields/{field}  (correction)   |
|    POST .../fields/{field}/revert   GET /api/shipments/{id}/corrections    |
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
   |                     categorical: incoterms · reference · country
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
make test           # full offline suite, zero API calls (socket guard, see Tests)
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
VISION_FALLBACK=true                 # re-read a failed document on the other host
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
- **`CONFIG_TOKEN` closes that gap when you need config to stay available.** Set
  it and both `/api/config` endpoints require a matching `X-Config-Token` header;
  the drawer prompts for it and keeps it in memory for the tab, never in
  storage. Comparison is constant-time, and an unauthenticated `POST` returns 401
  without applying anything. Left blank, behaviour is unchanged for local use —
  which is correct on a laptop and wrong on a shared host.

With the flag off, `GET /api/config` still reports status but `POST` returns 403
and the drawer explains where to set keys instead. A locked deployment answers
403 before it asks for a token, because on a locked deployment the fix is an
environment variable rather than a field to fill in.

### Transparency panel

Every result screen ends with a **Transparency** table: one row per document plus
rows for the second-reader consensus pass (when used), the executive summary, the
draft discrepancy email and each advisory second opinion — naming the provider,
the model, whether the answer came from a live call or the cache, attempts,
tokens and latency. The same table is printed in `report.html` and the PDF, so
all three views of one run agree on which model did what. Below it, **Raw model
output** lists each persisted body from `runs/{id}/raw/` and shows it verbatim.

The panel is built to be unflattering where the facts are. A run that made no
calls says so in an amber caveat rather than showing cached numbers silently, a
replayed document reports zero tokens, and a hand-authored fixture is never
labelled as model output. Cost appears only if you set `USD_PER_MTOK`; with it
unset the panel prints token counts and declines to invent a price. The
**"Powered by Nebius Token Factory + NVIDIA Nemotron"** badge carries the same
honesty: it states how many calls ran, and says "no live call" when the numbers
on screen were replayed from cache rather than made now.

`GET /api/shipments/{id}/raw/{name}` serves those bodies as plain text, and only
for filenames the report itself listed — a name the report never named is not
evidence about that run.

### Progress: pushed, with a poll behind it

`GET /api/shipments/{id}/events` streams two kinds of frame. `event: stage`
carries per-document progress — `queued`, `reading`, `verifying`, `comparing`,
`done`, `failed` — with the provider/model and the elapsed milliseconds; `event:
report` carries the full report each time it settles. Every report mutation
already funnels through `RunStore.write`, so that one hook is the fan-out point
— no second write path to keep in sync. The stream carries the same document an
immediate poll would, including a replay on subscribe, so a client that joins
mid-run sees the full picture rather than waiting for the next change.

The browser keeps the 1-second poll running until the first stream event proves
the channel is alive, then drops it. If `EventSource` is unavailable, the route
is blocked, or the stream errors, the interval is still there and the UI behaves
exactly as it always did. A stalled or disconnected client cannot slow a run
down: each listener is a bounded queue that drops its oldest frame, and the
generator unsubscribes in a `finally`.

The progress line counts documents settled and seconds elapsed, because a
reasoning vision model takes about a minute per document — measured live, omni
p50 58.8 s / p95 67.2 s across twelve documents — and a bare "Extracting…" reads
as a hang. Under it, a per-document timeline renders the stage frames — the
document, its current stage, the provider and model working on it, and elapsed
seconds. The timeline is advisory: it yields to the settled documents table the
moment the report is done, and a run whose stream dies still finishes correctly
via the poll path, with the timeline simply left empty.

### Cross-field sanity

Each field is reconciled on its own, so a shipment can be perfectly consistent
field by field and still be impossible. `app/rules.py` checks pairs:

| Rule | Fires when |
|---|---|
| `unit_mass_high` | gross weight ÷ count implies more than 2,000 kg per piece |
| `unit_mass_low` | it implies less than 0.0005 kg per piece — a pallet counted against a carton weight |
| `unit_price_high` | invoice value ÷ count implies over 250,000 per piece |
| `value_without_currency` | the value carries no currency code |
| `hs_code_short` | fewer than the 6 internationally harmonised digits |
| `net_above_gross` | net weight exceeds gross weight — packaging cannot have negative mass |
| `bill_of_lading_without_container` | a B/L was read but no document stated equipment |
| `single_source_fields` | states which fields were never cross-checked, so "unverified" is not mistaken for "agrees" |

All of it is arithmetic in Python over the engine's own canonical keys
(`weight:12450.000`, `money:USD:48200.00`, `count:48:ea`), which are ours to parse
because we wrote their format. The model still computes nothing. Every
explanation quotes both the number it derived and the named bound it broke, so a
reviewer can disagree with the threshold rather than trust the verdict, and a
`risk`-level finding raises the customs-risk strip.

Currencies are never converted, so `unit_price_high` compares like for like only —
the rule states that in its own text.

### Risk score: 0–100, with the arithmetic shown

The customs-risk strip is a number **and** its breakdown. `app/scoring.py` awards
points per rule — a conflicting HS code or container number is 60 and clears the
high band on its own, a weight conflict is 40 and lands in the middle, an
unreadable document is 35, each un-cross-checked field 5 — then reads the band off
the total (`≥55` high, `≥25` medium). The level is *derived from* the score rather
than decided alongside it, so the strip and the number cannot disagree;
`customs_risk()` is a thin view over the same function for that reason.

Three properties the tests pin down:

- **Capped and accounted.** `score == sum(contributions)` below 100, and the cap
  is the only thing that can break that equality above it — asserted both ways.
- **Volume cannot impersonate severity.** Caps (15 for coverage, 60 for sanity
  findings, scaled rather than dropped) mean five lightly-checked fields never
  outscore one bad HS code.
- **`customs_risk` is a view, not a second implementation.** Two places deciding
  severity is exactly how a score and a strip start disagreeing about one
  shipment.

Every point is shown next to the rule code and the value that triggered it, in the
UI and in the exported report. `method: "deterministic-v1"` is recorded on the
report so a score can be traced to the weighting that produced it even after the
weights change.

### Evidence you can navigate

Every value in the comparison table carries a **page N →** chip. It opens that
document's page image with the exact line the model quoted as evidence beneath it,
so a verdict can be checked against the source without hunting through uploads.
Escape or a click closes it.

**The line is drawn on the page, when the page can be asked where it is.** The
coordinates do not come from the model — the extraction contract still carries
`page_index`, `evidence_text` and no box, and adding one would be a prompt change
with the cache caveat under **Model discovery** attached to it unchanged. They
come from the PDF's own text layer, which already knows where every glyph sits:
`app/textlayer.py` walks the character array once (~6 ms for an 1,800-character
page, measured), unions each line's character boxes, flips the y axis and divides
by the page size. Percentages survive the ingest re-encode because the aspect
ratio is preserved, so the rectangle lands on the words rather than near them.

Three cases get no box, and the lightbox says so in words instead of leaving the
reader to notice an absence:

- **a photo or a scan** — there is no text layer, so there is nothing to place;
- **a quote that joined two table cells** (`row`) or several lines (`spread`) —
  the model's sentence is real, but no single printed line is that sentence;
- **a fuzzy match** — `partial_ratio` scores containment, so a quote naming a
  row's label *and* its value can score a perfect 100 against the value's line
  alone. Framing that line would claim the model copied something it did not.

A rectangle is therefore only ever drawn for a quote that appears verbatim inside
one printed line. The PDF cannot carry the overlay at all — the print renderer
composes text rather than drawing on the document image — so `report.pdf` gives
the page number and the quoted line and points at `report.html`, which embeds the
pages with the same boxes drawn on them.

### The quote is checked against the page

A value and the line it claims to come from are both just strings the model
produced, so until now the evidence column asked for trust rather than earning it.
`app/verify.py` checks each quoted line against text the model never touched: the
PDF's own embedded text layer, recovered at ingest by `app/textlayer.py`. A field
whose quote or value is not on the page is marked **unverified**, removed from the
comparison, and listed under **Unverified evidence** in the UI, the PDF and the
CSV. It is never silently dropped.

Four outcomes, because two would force a lie:

| State | Meaning | Effect on the comparison |
|---|---|---|
| `verified` | quote and value both found in the page's text | included |
| `unverified` | a text source exists and the quote or value is not in it | **excluded**, shown as absent |
| `not_checkable` | no text source — a photo, a scan, a flat image | included, labelled |
| `absent` | the field reports no value | nothing to check |

The distinction between the middle two is the whole design. Treating "we could not
look" as "we looked and it failed" would empty every report built from a phone
photo; treating it as "fine" would claim a check that never happened. So a scan
keeps its values and says plainly that nothing verified them.

Matching tolerates three things a naive check would wrongly reject, each found in
our own data rather than imagined: a quote that joins a table's label cell to its
value cell across a line break, a quote that joins a **column header** to a cell
(`3. HS CODE 8517.62` — NVIDIA produced exactly this, and rejecting it would have
discarded a correct extraction), and a value reformatted across number
conventions (`12.450,00` for a page printing `12,450.00`), which is compared with
grouping punctuation removed.

**What it cannot catch, and this is the important part:**

- **A real string filed under the wrong field.** Every one of the four fields our
  Nebius captures "invented" is this shape. The packing list's `po_number` came
  back as `HW-2026-0913` — that is the *invoice reference*, genuinely printed on
  that page. A bill of lading's `country_of_origin` came back as `China`, lifted
  from "Ningbo, China", a port of loading. Both gates pass, because the text
  really is there. Page text cannot see a misattribution; that is a semantic
  error, and no threshold change makes it detectable this way.
- **Anything on a photo or a scanned image.** There is no text layer to check
  against, so every field on the app's most common real input is `not_checkable`.
  **Invention on a photo or scan is undetectable by code in this build.** It is
  not detected here, it is not detected by a confidence score, and the report says
  so instead of implying otherwise.

Measured against the captures in this repository: all 156 present fields across
both providers verified — 152 matched a printed line, 3 matched across a row's
cells, 1 across a header and a cell — and none was fabricated outright. That is a
statement about those captures, not a general accuracy claim.

The threshold is `EVIDENCE_MIN_RATIO` (default 90, rapidfuzz `partial_ratio`). It
is not 100 on purpose: demanding an exact reproduction would fail honest quotes
while proving nothing about the value.

### Arithmetic inside a single document

Cross-document comparison asks whether the papers agree. It cannot ask whether a
paper is *self-consistent*, and a shipment can pass the first and fail the second:
every document can repeat the same wrong total and agree perfectly. So
`app/mathcheck.py` checks each document against itself, in pure Python:

| Check | What it proves |
|---|---|
| `line_total_math` | quantity x unit price equals the printed line amount |
| `line_total_sum` | the goods lines add up to the stated invoice total |
| `row_weight_sum` | the per-row weights add up to the stated gross and net |
| `net_above_gross_in_document` | a document's own net weight is not above its gross |

Three of the four need the goods lines, so they need `ENABLE_LINE_ITEMS=true`. The
fourth needs only two fields every document already states, and runs on every
shipment regardless.

**`not_run` is a status, not an omission.** Each line-dependent rule records
`pass`, `fail` or `not_run` per document, and the report, the PDF and the
exported README all print the not-run rows. A list that contained only passes
would let a shipment where nothing was checked look identical to one where
everything was.

Tolerances are in config, and the money one is deliberately absolute:
`MATH_TOLERANCE_MONEY=0.02`. A relative band would have missed the defect this
project found in its own fixture data -- a row printing `600 x 9.18 = 5,510.00`
where the product is 5,508.00 -- because 0.5 % of 5,508 is 27.

A line printed in a currency the total is not stated in **fails rather than
being added**, because adding unconverted columns is meaningless and converting
them is forbidden. And a row whose price has an ambiguous separator (`2.675`,
which the normalizer reads as 2675) is **set aside and named**, not judged:
reporting a reading limit as an arithmetic error in the document would be
attributing our own ambiguity to somebody else's paperwork.

### One severity per finding, and the banner that follows from it

Six panels can now comment on one shipment: the verdict, the field table, the
cross-field rules, the arithmetic, the goods lines, and the evidence checks. Each
answers its own question, and a reviewer was answering theirs by reading all six.
`app/severity.py` gives them one answer, by looking at every finding the other
stages already produced and assigning each a level from one table.

| Level | What earns it |
|---|---|
| **CRITICAL** | container number, consignee, HS code (first six digits), country of origin, or an invoice value stated in two currencies |
| **WARNING** | any other cross-document conflict, a suspicious reading, a document that could not be read, an internal arithmetic failure, a withdrawn unverified value, a cross-field rule at `risk`/`warn` |
| **INFO** | a difference of counting basis (items against cartons, whatever the two numbers are), the same count printed with different package words, a field only one document stated, a check that did not run, a goods line one document printed alone |

Country of origin is the newest entry in the critical band, and it is a judgment
call rather than a measurement: origin decides the duty rate and whether an FTA
preference applies at all, so a shipment whose papers name two countries cannot
be entered correctly while that stands. The 0-100 risk strip already scored it
that way (`origin_conflict` is worth 60 points, above the high band at 55) — the
severity table saying `warning` meant the two readers of one report disagreed
about how bad the same fact was. A *near-miss* on origin stays a warning; only an
outright conflict blocks. Both directions are pinned in `tests/test_severity.py`,
and the pin is measured rather than assumed: setting the row back to `warning`
breaks 2 tests.

`SEVERITY_TABLE` in that one file is the only place a level is decided -- tuning
severity is editing a dict, not chasing conditions. It is a table of *levels*, not
of scores: the 0-100 customs strip above stays where it is, because "how exposed
is this entry to customs" and "can this shipment move today" are different
questions with different answers.

The banner is mechanical: any critical → **BLOCKED**, else any warning →
**REVIEW NEEDED**, else **READY TO SHIP**. It sits above the verdict in the web
report, at the top of the PDF, and in `report.html`, each time with its counts and
with the verdict and risk score printed beside it -- so if they ever disagreed,
the disagreement would be visible instead of two panels apart.

**It can be stricter than the verdict, and never kinder.** Stricter is the point:
the evidence and arithmetic stages introduced findings a cross-document comparison
cannot see, so a shipment whose papers agree perfectly can be held because its own
invoice does not add up. Never kinder is a property, not a hope: `ready` is
unreachable while the verdict says `review` or `conflicts`, and unreachable while
the risk strip is at or above its medium band. `tests/test_severity.py` pins that
from both directions -- it enumerates the scoring table, so a rule added to the
risk strip without a severity row behind it fails a test instead of shipping a
banner that ignores it.

**What READY TO SHIP does not mean.** It means no rule found something to block.
It is not a guarantee that every value was cross-checked: on a shipment where only
one document states the invoice value, nobody compared it, and that is exactly the
case where an invented number survives. A phone photo of a printed invoice has no
text layer, so the evidence check in the section above cannot run on it at all --
that shipment can read `ready` with a value on it that no document prints. The
banner's own caveat line says this in the same words, and the informational count
beside it is how you can see how much of the report was never compared.

### Scored against ground truth the bundle declares in advance

`fixtures/planted.py` holds 19 synthetic shipments whose right answer is written
down before anything runs: 3 clean, 5 tricky-but-correct, 9 with **exactly one
planted error**, and 1 case the engine cannot catch. They read 43 document slots
off 21 distinct pages -- identical paperwork is rendered once, because a cache key
is a hash of the page images and one page can only carry one authored answer. `scripts/evaluate_planted.py`
runs all of them through the real pipeline and prints what it found:

```
planted errors caught   9/9
false alarms            0/9  (clean + tricky shipments not called ready)
designed miss           1 declared
wall clock              2.4s for 19 shipments (43 documents)
API calls made          0  (provenance: authored)
approximate cost        $0.00 -- every response is hand-authored, so this run
                        measures the engine, not a model
```

Two numbers, deliberately never averaged. **Caught** asks whether a wrong
shipment was stopped; **false alarms** asks whether a right one was. The second
decides whether anyone keeps using the tool, which is why a bundle that only
reported the first would be worthless.

The tricky group is the one that earns its place: pounds against kilos, a
European decimal comma, national HS digits beyond the harmonised six, a legal
suffix dropped, 1,200 pieces against 100 cartons. Each was a real defect found
during this project's development, and each is now a case that fails if it comes
back. `--check` makes the whole bundle a gate; `--write` regenerates
[`docs/PLANTED.md`](docs/PLANTED.md), which carries the per-case table and the
reasons.

**The miss is part of the score, not a footnote to it.** `miss_inside_tolerance`
plants a 600 g gap on a 12,450 kg shipment. The engine's absolute band is 1 kg —
chosen because two re-weighs of a container rarely agree to the gram — so the
report says the paperwork agrees, and it is wrong about the cargo. That is the
price of a tolerance, and a bundle with no miss in it is measuring its own code.

Everything here is authored, including the model responses, so the run is
reproducible and free. It says nothing about live accuracy; `docs/EVAL.md`
measures that from captured responses, and the two must not be confused.

### Correction drafts, written by rule

The report ends with somebody having to fix paperwork, and a reviewer retyping a
discrepancy into an email is how a wrong number leaves the building.
`app/correction_email.py` writes the drafts instead, from the severity findings the
banner already has. **No model is asked** — nothing can be added, softened or
invented between a verdict and the message a customer receives.

Each draft carries the value as printed, the value the other papers printed, and
the exact line it was quoted from. Only critical and warning findings are written
up: an informational item is not a request to change anything, and asking for it
would train the recipient to skim.

Recipients are **roles, not people** — "the carrier's documentation desk", "the
seller's documentation desk" — and every draft says on its first line that no
contact address is on file. A document type establishes who owes the paper; it does
not establish who to mail, and the app does not pretend otherwise.

Who gets written to is decided by evidence, not by order:

- a **strict majority** names the odd document, and only its party is written to;
- an **even split** writes to both, and says the report cannot decide between them;
- unless something on the papers *does* decide it. A container number that fails
  its own ISO 6346 check digit is inconsistent whatever the other paper says, and
  that verdict is a field on the comparison (`arbitrated_by`), not a sentence a
  generator has to parse out of prose.

The drafts ship everywhere the report does: a panel with a copy button in the web
UI, `corrections/*.txt` in the ZIP, a section in `report.html` and in the PDF. The
older AI-written briefing is still there, retitled and labelled as the optional
restatement it is; these are the default.

**What a draft never does is state an expected value.** It reports printings. Where
a number genuinely is derivable — the product of a quantity and a price — the draft
says it is *our calculation* and names the document whose own figures produced it.

### Corrections a human can make, and undo

Every value in the comparison table has a **Correct** button. Change it and the
verdict moves in the same round trip — `conflict` became `consistent`, the risk
score fell from 100 to 70 — because normalization, comparison, cross-field sanity
and the score are all pure functions of the values. **No model is called**, so
fixing a transcription costs nothing and returns immediately.

Three properties that make this safe rather than merely convenient:

- **The model's answer is never overwritten.** A correction lives in
  `document.overrides` beside `document.extraction`, which keeps what the reader
  returned verbatim. A corrected cell is marked, and says so: *"corrected by
  vishnu · model said 13,200.00 KGS"*. Reverting is just re-normalizing the
  original, which is why no "before" snapshot has to be trusted to stay correct.
- **The trail is append-only.** `GET /api/shipments/{id}/corrections` returns every
  value a human changed, who said they were, and what the model originally read.
  Reverting stamps `reverted_at` rather than deleting the entry — history you can
  quietly erase is not an audit trail.
- **Consequences are recomputed, not hand-patched.** After a correction the
  second opinions already recorded are carried onto the rebuilt comparisons and
  marked `superseded_at`, and a narrative written about the old verdicts keeps its
  text but gains the same stamp. Nothing that cost an API call is silently
  discarded, and nothing stale is presented as current.

Reverted entries, refusals and limits: an empty value is a deletion rather than a
correction and is refused (400), as is a field outside the ten; an unknown
shipment is 404. `actor` is free text, not identity — this app has no accounts, so
the trail records who they claimed to be, which is still worth more than nothing.

### Two readers, when you can afford the doubt

Set `CONSENSUS=true` (or tick **Second reader** in the Configure drawer) and
every page is also read by the *other* provider — NVIDIA
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

**Verified against the live NVIDIA host on 2026-10-02.** `GET /v1/models` on
`integrate.api.nvidia.com` returns 81 models as `{"object":"list","data":[...]}`,
each row carrying only `id`, `object`, `created` and `owned_by` — **no modality
field at all**, and `/v1/models/{id}` returns the same four. So on NVIDIA, image
capability can only be inferred from the model name; `Selection.inferred` records
that, and a report can never present an inference as a host declaration.
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` was confirmed present in that
catalog, so the default model is verified live and `model_selection` reports
`configured`.

**Verified against the live Nebius host too, and it corrected a bug.** Token
Factory puts its capability string at `architecture.modality` under
`?verbose=true`; `parse_catalog` was reading a top-level `modality` that neither
host returns, so **0 of 25 Nebius models appeared image-capable** and a host that
*states* its capabilities was being decided by a name guess. Measured after the
fix: 25 models, 25 declaring, 6 image-capable, and the retired Nemotron vision
entry now resolves to `openbmb/MiniCPM-V-4_5` as a declared capability.

The tests were part of the problem: they asserted payload shapes neither host
returns, so the suite stayed green while the live path found nothing. They now use
captured shapes, and a `text->embedding` row is pinned as ineligible for both
roles. **What is still unverified about Nebius:** nothing about its catalog — but
its summary-model behaviour is exercised far more lightly than its vision path, and
`make probe-live` remains the way to re-confirm either.

### Line by line, when the totals agree and the shipment is still wrong

`ENABLE_LINE_ITEMS=true` asks for a second, separate read of every page: the
goods table, transcribed row by row. Those rows are then matched in Python across
documents, and the report gains a **Goods lines** section naming *which line*
disagrees.

This exists because the scalar fields answer "do the totals agree", and that is a
weaker question than it looks. An invoice and a packing list can both print 100
cartons, 12,450 kg and the same value, while one lists 60 + 40 and the other 50 +
50. Every field comparison passes. The shipment is still wrong.

What it deliberately does not do:

- **It cannot move a verdict.** The section is written after the comparisons, the
  risk score and the shipment state are settled and persisted, and nothing in the
  engine reads it. `tests/test_lineitems.py` runs the same fixture twice and
  asserts the comparisons, summary, risk contributions, sanity findings and every
  document are byte-identical with the flag on and off.
- **It never matches on similarity.** Two rows are the same row when they carry
  the same item number, or when both carry none and their descriptions fold to
  the same tokens (case, accents, punctuation, word order and package words
  removed; every number kept). A fuzzy band would score `H-40` against `H-41` as
  a near match and delete the only finding in the room. A spelling variant of a
  material name therefore shows up as two rows, which is a thing a person can
  resolve, rather than one row, which is not.
- **It counts a package-basis difference separately from a conflict.** 60 PCS
  against 60 CTNS is reported as `package basis differs`, not as a disagreement
  about how much was shipped.
- **It says which documents were not read.** A bill of lading with no goods table
  is never described as "not listing the item" — nobody read its lines, and the
  detail says so instead of implying a comparison that did not happen.
- **It has its own cache contract.** The key is
  `sha256("lineitems-v1", provider, model, page_hashes)`, and the hand-authored
  fallback tier filters on that tag. Without the filter, a goods-lines fixture
  would answer a ten-field lookup on the same pages, and the shape would parse
  with every field absent — a silent, plausible-looking empty report.

**Verification status, stated plainly: the line-item read has never been run
against a live model.** No fixture page has captured goods lines, so in
`DEMO_MODE` the section reports `skipped` with that reason rather than showing an
empty table. What *has* been verified is the plumbing, offline and through the
running app: parsing and repair, the cache namespace, the pipeline's inability to
change a verdict, and the UI and PDF rendering the section from a hand-seeded
cache — which the panel then labels `hand-authored fixture — no model read these
lines`. Capturing real goods lines is a `make probe-live`-shaped follow-up, and
until it happens the feature's accuracy against a model is unmeasured.

Off by default because it is one more vision call per document.

### Extracting live

A real single-document call on 2026-10-02, through the shipped pipeline, on
`fixtures/images/invoice_conflict.png`:

| | |
|---|---|
| Capability probe | `honours strict` → `json_schema` mode on NVIDIA, decided by the probe, not assumed |
| Result | `status: ok`, `source: live_api`, parsed on ladder rung 1 — no correction, no simplification |
| Tokens | 3,145 prompt + 1,057 completion = **4,202** |
| Model latency | **35.1 s** for the call; ~190 s wall including the probe |
| Raw body | 965 bytes written to `runs/{id}/raw/doc1_initial.txt` |
| Fields | 100 CARTONS → `100 cartons`; 12,450.00 KGS → `12450.000 kg`; 8517.62.00 → `85.17.62 (+2 national digits)`; USD 8,420.00 → `8,420.00 USD` |

Every value above is what the app recorded about its own call, which is the point
of section **Transparency**. That 35.1 s is a single early document; the twelve
document live run behind `docs/EVAL.md` puts omni at p50 58.8 s / p95 67.2 s, and
the ~190 s wall above for the first document is why discovery and the probe are
cached per process.

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

**What the real hosts publish** is now recorded above, under **Verified against
the live NVIDIA host** — including the fact that NVIDIA publishes no modality at
all, which is why `Selection.inferred` exists, and the Nebius `architecture.modality`
reading that was wrong until it was measured against the live catalog. Both sides
have been checked against a real host; `make probe-live` re-checks them.

### The adjudicator: a second opinion, never a second decision

Two of the ten fields are genuinely interpretive — whether "ACME Freight Ltd" and
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

The same advisor writes a one-line note beside each value the evidence check
withdrew, guessing only at *why* the quoted line was not found (a page-break
reflow, an OCR ligature). It is labelled "AI-generated, advisory", cached by the
evidence rather than the document, and silent on any failure — and it never
reinstates the value. Both directions are pinned with a fake transport: the
withdrawal is byte-identical advisor-on vs advisor-off, and a failing advisor
never fails the run.

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
docker run -p 8000:8000 -v sdr-runs:/app/runs shipment-doc-reconciler   # live; paste keys into Configure
docker compose up --build                                               # same, with a named runs/ volume
docker run -p 8000:8000 -e NVIDIA_API_KEY=... -e NEBIUS_API_KEY=... \
  shipment-doc-reconciler                                  # live, keys from the environment
docker run -p 8000:8000 -e DEMO_MODE=true shipment-doc-reconciler   # keyless cache demo
```

The image declares a `HEALTHCHECK` and runs as an unprivileged user;
The whole surface is HTTP: [`docs/API.md`](docs/API.md) is the endpoint
reference, the `API_KEYS` contract, and working curl / Python / JavaScript
clients for a run.

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) covers Render, Fly/Railway, what the
health endpoint does and does not claim, and why there is no hosted URL yet.

Mount `runs/` (or set `RUNS_DIR` to a persistent path). The filesystem is the
database: `report.json`, the page JPEGs, the uploads and the raw model bodies all
live under it.

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

**No bounding boxes from the model.** VLM box coordinates are unreliable, and a
rectangle the model *guessed* would dress up a guess as precision. The highlight
in the evidence viewer is therefore measured, not asked for: it comes from the
PDF's own text layer, and only for a quote that appears verbatim inside one
printed line. Where the page has no text layer, the evidence stays what it always
was -- a verbatim line, a page number and the viewable page -- and the viewer says
there are no coordinates rather than drawing a shape anyway.

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
report.pdf     the same report laid out for print and filing (also served
               on its own at /api/shipments/{id}/report.pdf)
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
all tests passed (~45 s here)
```

~36 s is expected and is CPU work, not waiting: `test_pipeline`, `test_api`,
`test_severity` and
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
| `test_normalize_categorical.py` | Incoterms, PO references and origin: what must unify, what must never be guessed |
| `test_prompt.py` | every contracted field is asked for, on every rung of the ladder |
| `test_compare.py` | tolerance bands, cardinality gate, arbitration, outliers |
| `test_parse.py` | retry-ladder rungs 1–3, "one bad field ≠ dead document" |
| `test_client.py` | payload shape, probe, ladder call counts, backoff, cache |
| `test_pipeline.py` | all three fixtures end to end through DEMO_MODE |
| `test_api.py` | real HTTP against the FastAPI app, ZIP contents, traversal |
| `test_api_auth.py` | the API-key gate, the session cookie, and a scan proving no data route ships unprotected |
| `test_pdf.py` | the PDF's text layer, its provenance wording, and markup injected from a document |
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
| Normalizers, comparators, parse ladder | ✅ the offline suite is green, and the browser suite runs against a real server |
| The status banner never contradicts the verdict or the risk strip | ✅ pinned from both directions, and proved to fail: flipping the container row from critical to warning breaks 8 tests, flipping the country-of-origin row back to warning breaks 2, and removing the verdict floor breaks 3 |
| Correction drafts name no value no document printed | ✅ asserted over every draft, and the ZIP manifest bug that hid README.txt's header is pinned |
| The engine against known ground truth | ✅ 9/9 planted errors caught, 0/9 false alarms on clean and tricky paperwork, 1 miss declared in advance — `docs/PLANTED.md`, 0 API calls |
| The evidence highlight lands on the right words | ✅ screenshot-verified in chromium (the rectangle and the image share one box), and proved to fail: dropping the verbatim-line restriction breaks the fuzzy test, and stopping the chip from passing its box breaks the browser suite |
| `runs/{id}/report.json` is the same bytes on Windows and in the container | ✅ was not — pydantic writes unescaped non-ASCII, and `write_text()` used the locale codec, so an em dash landed as `0x97`. Encoding is now pinned to utf-8 on write and read |
| End-to-end pipeline, UI, ZIP export, HTTP API | ✅ cache-verified **and** live-verified |
| Vision extraction, NVIDIA build.nvidia.com | ✅ 9/9 fixture documents; 11 calls, every one parsed on ladder rung 1 |
| Vision extraction, Nebius MiniCPM-V (fallback) | ✅ 9/9 documents, all parsed on attempt 1 |
| Nebius summary call (the hackathon requirement) | ✅ live, one call per shipment |
| `response_format: json_schema` (strict) | ✅ accepted on **both** hosts — wrapped shape only |
| Base64 data URIs | ✅ accepted on both |
| Checksum arbitration on live output | ✅ flagged the planted bad container number |
| Cross-page evidence (`page_index`) | ✅ live model reported page 2 correctly |
| Extraction determinism | ⚠️ **not guaranteed.** Two calls on one page were byte-identical, but a 6-call re-capture on 2026-10-10 moved NVIDIA's `incoterms` *value* on 2 of 3 invoices and shortened 2 evidence *quotes*. Nebius was byte-stable across the same re-capture. `compare_key` exactness (75/75) and every shipment verdict were unchanged. |
| Pricing / context / rate limits | ✅ live `GET /v1/models?verbose=true` (Nebius) |
| `nvidia/nemotron-nano-12b-v2-vl` (brief's model) | ❌ **410 Gone on build.nvidia.com**, 404 on Nebius |
| `docker build` | ⚠️ **never run** — no Docker daemon in this sandbox |

### Live findings that changed the code

- **Re-capturing the same contract moved the verbatim score.** On 2026-10-10 the
  three invoice pages were re-captured on both providers after a fixture
  correction. NVIDIA's answers came back different on two of them: the
  `incoterms` value dropped its parenthetical (`FOB Ningbo (Incoterms 2020)` →
  `FOB Ningbo`), and two evidence quotes came back shortened to the value alone
  (`CONSIGNEE Nordwind Industrial Supply GmbH` → `Nordwind Industrial Supply
  GmbH`). Nebius was byte-stable on all three. Measured effect: NVIDIA verbatim
  **74/75 → 72/75**, `compare_key` exact **unchanged at 75/75**, and no shipment
  verdict moved. Two things follow. Verbatim accuracy is a property of a capture
  run, not a stable capability of the model, so `docs/EVAL.md` should be read as
  "these captures", not "this model". And the evidence verifier cannot demand
  that a quote reproduce its printed line exactly — the model does not even agree
  with itself on how much of the line to copy — so matching is substring-or-fuzzy
  against the page, with the *value* checked separately.

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

- **Live round, 2026-10-19, under NVIDIA capacity pressure.** The omni host
  returned intermittent `503 ResourceExhausted` *and* silent hangs — requests
  that opened a connection and never sent response headers, which the per-request
  timeout does not bound. A document that exhausts the 5-attempt retry ladder is
  honestly marked failed, and when the failed document carried the key conflict
  the verdict weakened from `conflicts` to `review` rather than guessing. The
  same pressure made a 90°-rotated, sheared, darkened phone photo return nothing
  within 150 s on two attempts, while a clean invoice answered in 40 s.
- **MiniCPM-V downgrades `strict` → `json_object` live.** Its strict probe
  "accepted but returned unusable JSON", which the probe ladder already handles
  and tests pin; consensus therefore runs on `json_object`.
- **Non-English labels now map, measured live on 2026-10-21.** The German case
  used to return `Warennummer` (HS code) and `Bestellnummer` (PO) as absent
  because the prompt named only English surface forms. With the German/French
  synonyms in the v5 hints, Nebius MiniCPM-V read the German case **10/10**
  fields and the French case **10/10**, zero invented on both. The quantity
  normalizer also learned `KARTONS`/`COLIS`/`PALETTEN` so a German invoice and
  an English packing list compare on the same unit instead of "unknown". NVIDIA
  omni hung past 150 s on the German case, so no omni number is claimed.
  **Both pages are synthetic** — `scripts/build_stress_inputs.py` draws them, which
  is why an exact score is possible at all; neither is a translated real document.
- **Preprocessing a bad phone photo measurably helps.** The same rotated,
  sheared, darkened, scribbled-on photo, same model (MiniCPM-V), once with
  `PREPROCESS_ORIENTATION=false` and once with it on: **6/10 fields and one
  invented value before; 8/10 and zero invented after**. The two remaining
  misses are a misread HS digit and the total hidden under the stamp. NVIDIA
  omni hung on both variants, so again **there is no omni number for this case**.
  The "photo" is a synthetic page run through the degradation filters in
  `scripts/build_stress_inputs.py`, not somebody's phone camera.
- **A candidate default was tested and rejected against its own rule.**
  `nvidia/Nemotron-3.5-Super-VL` on Nebius was measured end to end on 2026-10-23
  (`scripts/live_super_verify.py`, four passes: consensus off and on, each twice):
  **3/3** shipment verdicts and **0** invented fields in every pass, but
  **74/75 (98.7%)** fields exact on the demo documents versus omni's **75/75** —
  and the same single miss reproduced in all four passes. The written promotion bar
  required exact ≥ 99%, so **omni stays the default** and Super-VL stays
  selectable. Its verbatim drifted (71 to 73 of 75) while `compare_key` exact did
  not move: the model transcribes loosely and the deterministic layer absorbs it.
- **A failed document is re-read on the other host.** Live check on 2026-10-21
  (`scripts/live_fallback_check.py`): with the primary NVIDIA endpoint pointed
  at a dead port, the document still arrived — `ok`, stamped
  `nebius / openbmb/MiniCPM-V-4_5`, with the routing note in the report.
- **Goods lines are the dominant live cost.** Four more omni calls per shipment
  took ~6–7 minutes on a degraded host — far more than extraction or the summary
  — because the same slow primary model is reused for the opt-in line read.


## Layout

```
app/
  config.py            pydantic-settings; both keys; version tags; tolerances
  providers.py         endpoint routing: vision vs summary, per provider
  fields.py            the frozen ten-field set; hints, kinds, expectations
  models.py            every Pydantic model + the hand-written JSON Schema
  prompt.py            system prompt, field spec, page markers, corrections
  client.py            ChatClient base; ExtractionClient + SummaryClient;
                       capability probe, ladder, per-provider backoff
  summary.py           executive-summary prompt + plausibility guard
  adjudicate.py        second-opinion prompt, and the guard on its answer
  narrative_cache.py   summaries keyed on a verdict signature
  parse.py             ladder rungs 1-3 (free, local)
  cache.py             sha256(prompt_ver + PROVIDER + model + page hashes)
  ingest.py            EXIF/resize/JPEG; pypdfium2 for PDFs
  textlayer.py         the PDF's own text layer, recovered without the model
  verify.py            is the quoted line actually on that page?
  normalize/           number weight quantity container hs_code party money categorical
  compare/             comparators, tolerance table, cardinality gate, reasons
  rules.py             cross-field sanity: the pairs one field cannot see
  mathcheck.py         arithmetic inside one document
  lineitems.py         goods-line matching: exact keys, never a fuzzy merge
  scoring.py           the 0-100 strip, every point attributable
  severity.py          the one level table, and the banner that reads it
  correction_email.py  the drafts it writes, by rule and with no model
  orchestrator.py      gather over documents; report assembly; summary step
  pdf.py               the print-and-file rendering, from the model not the DOM
  export.py            ZIP: report.json + report.csv + report.html + README.txt
  store.py             ./runs/{id}/ - filesystem as database, atomic writes
  limits.py            the sliding window on the one endpoint that spends money
  main.py              FastAPI routes; /api/health reports both providers
web/                   index.html . app.js . app.css   (no build step)
fixtures/
  templates.py         the four document types as HTML
  shipments.py         one dict drives both the page and its authored response
  images/              12 uploads (13 pages; one is a 2-page PDF)
  text/                12 sidecars (13 pages) of page text, for the evidence check
  responses/           36 entries / 12 documents:
                         12 nvidia live . 12 nebius live . 12 hand-authored
  responses/narratives/  3 real AI summaries, keyed on verdict signature
  planted/               the scored bundle: 19 shipments over 43 document slots
                         on 21 distinct pages, each case declaring its ground
                         truth in fixtures/planted.py
  live_responses/      captured live responses + raw bodies (incl. a
                       duplicate call kept as a determinism datapoint)
scripts/
  build_fixtures.py    render -> degrade -> ingest -> author cache -> text sidecars
  selfcheck.py         demo rehearsal, zero API calls
  evaluate.py          accuracy against the captured live responses
  evaluate_planted.py  score the engine against the planted-error bundle
  probe_live.py        verify BOTH providers against the real APIs
  live_compare.py      live extractions diffed against the authored cache
  capture_summaries.py write real Nebius summaries into the demo cache
  migrate_cache_keys.py re-key the cache after a format change
tests/                 offline tests (+ conftest.py socket guard), green as a suite
tests_e2e/             browser tests; a real uvicorn and real chromium, and the
                       only suite allowed to open a socket. `pytest tests/` cannot
                       collect it; run `make e2e`.
docs/                  ARCHITECTURE.md (shape + invariants) · NORMALIZATION.md (every
                       rule and threshold) · PROVIDERS.md (what each host really does)
                       · EVAL.md (generated accuracy) · SECURITY.md (threat model) ·
                       AUDIT.md (findings, evidence) · DEPLOYMENT.md · API.md ·
                       adr/ (nine decision records)
```

The three-layer cache is deliberate. Each fixture document has a **NVIDIA** live
response, a **Nebius** live response, and a **hand-authored** fallback. Any
`VISION_PROVIDER` setting finds a real answer, and provenance is labelled
honestly in the report — replaying an authored fixture is never reported as a
model result.


## License

MIT.
