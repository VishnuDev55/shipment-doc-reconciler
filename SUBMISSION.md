# Submission — Shipment Doc Reconciler

**Track: Best Apps and Agents**
**Hackathon: Nebius × NVIDIA Global AI Hackathon**
**Team: solo** · **License: MIT**

---

## What it is

Upload the paperwork for one shipment — commercial invoice, packing list, bill
of lading — as scans or phone photos, and the app reads six shipment-level
fields out of each document, converts them to a common basis in deterministic
Python, and reports where the documents **disagree**, with the source line
quoted as evidence.

It answers the question that currently gets answered by a human opening three
PDFs and eyeballing them: *do these three documents actually describe the same
shipment?*

---

## The problem it solves

A single shipment is documented three times, by three different parties, for
three different purposes. The exporter's invoice, the freight forwarder's
packing list, and the carrier's bill of lading are all supposed to describe the
same goods, and in practice they do not.

This is a well-documented operational failure mode rather than an invented one.
Practitioner sources on customs entry errors consistently identify the same
pattern: **cross-document reconciliation** between the commercial invoice,
packing list and bill of lading is done manually before filing, and a single
mismatched field — an HS code, a consignee, a weight, a value — propagates into
cargo clearance delays, customs inspections, entry amendments, penalties and
shipment holds
([Tier2 Systems](https://tier2systems.com/en/blog/shipping-document-errors-delays/),
[ImageToTable](https://imagetotable.ai/blog/customs-declaration-data-entry-errors-delays),
[Stile International](https://stileintl.com/when-customs-data-does-not-match/)).
US CBP penalties for incorrect entry data specifically cover errors traceable to
document extraction, including understated or overstated values and wrong
classification
([source](https://invoicedataextraction.com/blog/customs-broker-invoice-processing)).

**The hard part is not reading the documents.** It is deciding whether a
difference is an *error* or just a *different way of writing the same thing*.
Trade documents are genuinely international, and a naive diff produces a wall of
false alarms on perfectly normal paperwork:

| Invoice says | Bill of lading says | Naive diff | This app says |
|---|---|---|---|
| `12,450.00 KGS` | `12.450,00 kg` | conflict | ✓ same — European decimal comma |
| `12,450.00 KGS` | `27,447.00 LBS` | conflict | ✓ same mass — 0.25 kg rounding |
| `1,200 pieces` | `100 cartons` | conflict | ⚠ different package basis, not an error |
| `8517.62.00` | `8517.62.0090` | conflict | ✓ same — HS is harmonized to 6 digits |
| `Nordwind … GmbH` | `Nordwind Industrial Supply` | conflict | ✓ same — legal suffix stripped |
| `MSKU3067322` | `MSKU3067328` | conflict | ❌ conflict, **and it names which one is wrong** |

### The strongest feature: a field that validates itself

A container number carries an **ISO 6346 mod-11 check digit**, so it is
self-verifying. When two documents disagree and exactly one number fails its own
checksum, the app can say which document is probably wrong with **no ground
truth and no second model call**:

> container numbers disagree: packing_list_conflict.png=MSKU3067328 [checksum
> FAILS], bol_conflict.png=MSKU3067322 [checksum OK]. The ISO 6346 check digit
> ARBITRATES: packing_list_conflict.png prints check digit 8 where the algorithm
> computes 2, so that value is internally inconsistent. MSKU3067322
> (bol_conflict.png) is the more likely correct container.

This fires on **live model output**, not just on hand-written test data —
verified against the standard's own published worked example
(`CSQU305438` → weighted sum 6185 → check digit 3).

---

## How it works

```
uploaded pages
   │  EXIF-orient → RGB → resize to 1600px → JPEG
   │  (PDFs rasterised with pypdfium2 — no poppler, no system deps)
   ▼
[1] VISION EXTRACTION          nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
    one call per document          @ NVIDIA build.nvidia.com
    all pages in the same call     strict json_schema (verified live)
    verbatim strings only — the model never converts a unit or does arithmetic
   ▼
[2] NORMALIZE                  pure Python, zero model calls
    number parsing (US/EU/space/apostrophe grouping, ambiguity flagged)
    weight → kg · quantity → count + package basis · ISO 6346 checksum
    HS code → first 6 digits · party name → accent-folded, suffix-stripped
    money → currency + amount (never FX-converted)
    emits canonical (for humans) AND compare_key (for the engine), together
   ▼
[3] COMPARE                    pure Python, deterministic
    cardinality gate → tolerance table → per-kind comparators
    templated reason strings with the real numbers interpolated
    outlier attribution when 3+ documents disagree
   ▼
[4] EXECUTIVE SUMMARY          nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
    one optional call per shipment   @ Nebius Token Factory
    restates the verdicts above; can never change one, and a failure here
    cannot break a shipment
   ▼
report.json  →  polled UI  →  ZIP export (JSON + CSV + standalone HTML)
```

**Design decisions worth stating out loud:**

- **The model transcribes; Python reasons.** Every value is a verbatim string.
  A hallucinated calculation is structurally impossible, and the audit trail
  survives.
- **No OCR fallback.** Layering Tesseract/PaddleOCR under the VLM means
  adjudicating between two extractions that disagree — a multi-day rabbit hole
  for no demo gain. The signal OCR would provide ("this value is suspect") is
  already produced three ways: the ISO 6346 checksum catches misreads with
  certainty, model confidence flags illegibility, and normalizer warnings flag
  ambiguity. This is a **design decision, not an omission**.
- **No bounding boxes.** VLM box coordinates are unreliable and the UI around
  them is fragile. Evidence is a verbatim quoted line + page number + the
  viewable page image — enough to verify a value in about two seconds.
- **Filesystem over a database.** No queries, no relations, no multi-user
  access. `runs/{shipment_id}/` *is* the artifact; the ZIP is that directory
  compressed.
- **Six scalar fields, not line items.** Per-SKU matching is future work.

---

## Hackathon requirements: exactly how each is met

Two **separate providers**, making **two separate kinds of call**, doing **two
different jobs**. This is stated plainly because it is not obvious from a skim
of the code.

| Requirement | Implementation | Runtime call |
|---|---|---|
| **Runs on Nebius Token Factory** | The **per-shipment plain-English executive summary** calls `POST https://api.tokenfactory.nebius.com/v1/chat/completions` using **`nvidia/Nemotron-3-Nano-30B-A3B`** | ✅ yes — verified live |
| **Uses at least one NVIDIA open-source model** | **Document extraction** — the core pipeline — uses **`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`** via NVIDIA's `build.nvidia.com` API. The Nebius summary model above is **also** an NVIDIA open model (Nemotron), just hosted by Nebius. | ✅ yes — verified live |

**Division of labour, explicitly:** the *primary* functionality (vision
extraction of all six fields from every document) runs on **NVIDIA
build.nvidia.com**. The **Nebius Token Factory** role is the reasoning/summary
feature. Both are runtime calls made by the running application, not build-time
or offline steps.

**Optional vision fallback:** setting `VISION_PROVIDER=nebius` moves document
extraction to Nebius Token Factory as well, using
**`openbmb/MiniCPM-V-4_5`** — the only `text+image→text` model available on the
Nebius key used during development. This path is also live-verified (9/9 fixture
documents). It is a **documented fallback, not the default**; the default is the
NVIDIA Nemotron omni model.

**Why the split exists:** the brief's model `nvidia/Nemotron-Nano-V2-12b-VL` is
unreachable with the keys available — 404 on Nebius (no Nemotron vision model on
that account; all four `nvidia/*` models report `modality: text->text`) and
**HTTP 410 Gone** on build.nvidia.com (`end of life 2026-08-26`). Rather than
ship a vision pipeline that cannot run, extraction moved to NVIDIA's own hosted
API where a Nemotron vision model *is* reachable, and the Nebius requirement is
met by a genuine runtime inference call. Full detail in `PROGRESS.md`.

### Runtime calls vs. cached replay — no overclaiming

Both provider calls are implemented as **runtime calls in the application code
path** and were each verified against the live APIs.

The app **runs live by default**. `DEMO_MODE=true` is the deliberate keyless mode
that the hosted submission instance runs in, and it matters:

- With no keys, the app replays a bundled cache containing **real captured model
  output** — 9 documents extracted by the NVIDIA Nemotron omni model, the same 9
  by Nebius MiniCPM-V, and 3 AI-written summaries from the Nebius Nemotron model.
  These are genuine model responses, not hand-written placeholders.
- The app **labels provenance honestly and will not overclaim.** Each report
  records whether values came from a call made *this run* (`live`), from a
  *previously captured* model response (`model-backed, replayed from cache — no
  API call made for this run`), or from a *hand-authored* fixture
  (`hand-authored, not produced by a model`). The UI and the exported HTML both
  say which. A cached hand-authored value is **never** reported as a live result.

So: a judge opening the hosted demo without keys sees real model output
replayed, and the app says so on screen. Running it without that flag, or with
`DEMO_MODE=false` and both keys supplied — through the environment or the
in-browser **Configure** panel — makes every request a genuine live call to both
providers.

---

## Track

**Best Apps and Agents.** Confirmed correct for the final two-provider build:
this is an application, not a model or dataset contribution. It is agentic in the
specific sense that a vision-language model performs document reading and
classification across multiple documents per shipment, an LLM performs a
reasoning/summarisation step, and the two are orchestrated into a workflow with
retry, caching, per-document concurrency and failure isolation. The
deterministic core is deliberate — an agent whose arithmetic is unreliable is a
liability in a compliance context.

---

## Feedback on the Nebius and NVIDIA platforms

Specific, from actually integrating both. Not generic praise.

### 1. Both platforms' documentation showed a `json_schema` payload that both platforms reject

This was the single most time-consuming issue, and it produced a **silent false
negative** in my own code.

Both docs show `response_format` taking a **bare** JSON Schema under
`json_schema`. Both live endpoints reject it and actually require OpenAI's
standard **wrapped** form `{name, strict, schema}`:

```
Nebius Token Factory   -> 422 {"detail":[{"loc":["body","response_format","json_schema","name"],
                                 "msg":"Field required"}]}
NVIDIA build.nvidia.com -> 400 {"error":{"message":"missing field `name`"}}
```

The error messages were at least *accurate and actionable* — `missing field
name` told me exactly what was wrong. The problem is that the documented example
is not what the API accepts, so anyone copying it gets a confusing 4xx and may
wrongly conclude the feature is unsupported. **Please fix the docs, or the
wrapper.** The fact that both platforms share this discrepancy suggests a doc
template rather than two independent mistakes.

Second-order damage worth flagging: my startup capability probe reported
"strict json_schema rejected" for a model that supported it perfectly, because
the request was malformed. A probe cannot distinguish *unsupported* from
*badly asked*.

### 2. NVIDIA's model card did not mention its own model's retirement

`nvidia/nemotron-nano-12b-v2-vl` on build.nvidia.com renders a full model card —
description, benchmarks, input/output specs — with a footer reading "Updated
about 2 months ago." Nothing on the page indicates the model is gone. Calling it
returns:

```
410 {"title":"Gone","detail":"The model 'nvidia/nemotron-nano-12b-v2-vl' has
      reached its end of life on 2026-08-26T09:00:00Z and is no longer available."}
```

Two concrete asks: mark retired model pages as retired, and **include retired
model IDs in the `GET /v1/models` response** (or a `/models?include_retired`)
with an `end_of_life` field. Right now a retired model is invisible in the
listing and only discoverable by guessing the exact ID and getting a 410 — which
is precisely how a hackathon team ends up specifying a dead model.

### 3. The Nemotron `content` vs `reasoning_content` split is a footgun

Reasoning models returned the answer in **three different shapes** across the
two platforms:

| Provider / model | `content` | reasoning field |
|---|---|---|
| NVIDIA omni | populated | `reasoning_content` also populated |
| Nebius `NVIDIA-Nemotron-3-Nano-30B-A3B` | **`None`** | `reasoning` |
| Nebius `Nemotron-3_5-Lightning` | chain-of-thought **leaked into `content`** | `reasoning_content` |

Naively reading `message.content` — which every OpenAI SDK example and tutorial
does — yields an **empty string on HTTP 200** for the Nano model, and a wall of
self-talk for Lightning. That is a silent data-loss failure mode: the request
succeeded, the status is 200, and you have nothing.

The third row is the worst, because it *looks* like a successful response.
Lightning ignored "no preamble, plain sentences" and returned its planning
process as the answer, hitting the token cap. I ended up needing a plausibility
guard that rejects output by *shape* (no terminal punctuation, chain-of-thought
markers, excessive length) — a hack I should not have to write.

Asks: document the reasoning-field behaviour per model, and consider a
`reasoning_effort: "none"` / `/no_think` parameter that is honoured consistently
rather than relying on a prompt token that one model respected and another
ignored. `/no_think` did measurably reduce reasoning tokens on Nano (~1218 →
1013), so it works — but it is undocumented in the chat-completions reference.

### 4. 503 `ResourceExhausted` on NVIDIA is indistinguishable from a real failure

```
503 {"error":{"message":"ResourceExhausted: Worker local total request limit
      reached (16/16)","code":503}}
```

Observed repeatedly, and **transient** — the same request succeeded on the 5th
attempt seconds later. Two problems:

- The counter in the message (`16/16`, and once `326/16`) is a *worker* metric
  unrelated to my account, so it tells me nothing actionable.
- No `Retry-After` header is returned, so clients must guess a backoff. I
  settled on 2/5/10/20/30s for this provider.

A distinct status code (429/503) plus `Retry-After` would let clients behave
correctly. As it stands, a naive client reads "503 on the model I asked for" and
concludes the model is unavailable.

### 5. No quota or credit visibility on NVIDIA's side

`GET /v1/models` and every chat response return **no rate-limit, quota, or
balance headers at all** — I checked explicitly. Nebius by contrast returns a
complete set (`x-ratelimit-limit-requests`, `-remaining-*`, `-reset-*`,
dynamic-scaling headers) that made it trivial to budget. On NVIDIA the only way
to know remaining credits is the dashboard, which is not scriptable. For a
hackathon where teams burn a trial allocation, a `x-ratelimit-remaining-*`
equivalent would be genuinely useful.

### 6. Latency of a reasoning VLM is the real product constraint

Measured per single-page document on NVIDIA: **34–88 s, median ~51 s**. MiniCPM-V
on Nebius was **2.7–4.0 s** for the same pages — an order of magnitude faster,
with comparable extraction accuracy on my fixtures (MiniCPM was in fact *less*
accurate: it invented a digit; the omni model transcribed correctly).

Both are legitimate observations, but the practical consequence matters: a
reasoning model's accuracy gain can cost more than a demo or a batch job can
spend. A documented `enable_thinking=false` / non-reasoning variant would let
builders choose per task rather than paying 51 s for a transcription job.

### 7. What worked well

Worth saying because it is true: **both APIs are genuinely OpenAI-compatible in
the way that matters.** One `AsyncOpenAI` client, only `base_url` and the key
differ. Vision via `image_url` with base64 data URIs worked on both with no
format surprises. Both honoured strict `json_schema` once the envelope was
right, and the structured output was clean enough that my parse-repair ladder
was never needed — every one of 18 live extractions parsed on the first rung.
Nebius's `GET /v1/models?verbose=true` returning **pricing, context length,
modality, quantization and per-model rate limits** in one call was outstanding;
it is how I discovered the Nebius account had no NVIDIA vision model at all, and
it should be the model for everyone else's catalog endpoint.

---

## Newly created during the submission period

**Yes.** This project was created from scratch during the Nebius × NVIDIA Global
AI Hackathon submission window (2026-09-22 → 2026-09-23). It is not derived from,
based on, or a modification of any pre-existing project, repository, or codebase
of mine or anyone else's. Every source file, test, fixture, document and script
in the repository was written during this period for this hackathon.

Development sequence, for the record:

1. API research against Nebius docs; scaffold and connection verification.
2. Schemas, normalizers, comparison engine — test-first, no API key available.
3. Synthetic fixtures + `DEMO_MODE` response cache; full application built and
   tested offline (201 tests).
4. Nebius key provided → live verification, which exposed that the brief's model
   is unavailable on that account and corrected the `json_schema` envelope.
5. NVIDIA key provided → two-provider architecture, live verification of both
   paths, and the platform feedback above.

---

## Repository contents

```
README.md          setup, architecture, requirements compliance, verification
SUBMISSION.md      this file
PROGRESS.md        running log: what was verified, every bug found, open items
VIDEO_SCRIPT.md    shot-by-shot plan for the <=3 min demo
LICENSE            MIT
Dockerfile         single-stage, python:3.11-slim, no apt-get (built and run)
render.yaml        Render Blueprint, free tier, DEMO_MODE=true
.env.example       both keys, with signup URLs and comments
Makefile           setup / dev / test / probe / probe-live / dist
app/               FastAPI, clients, normalizers, comparison engine, export
tests/             297 tests, ~10s, zero live API calls (enforced by a socket
                   guard in tests/conftest.py, itself under test)
fixtures/          9 synthetic documents + 3-layer response cache
scripts/           fixture builder, demo self-check, live probes, migration
docs/              AUDIT.md — what was verified, what is broken, what is deferred
```

`DEMO_MODE` defaults to **false**, so the app calls the live providers; keys come
from the environment or from the in-browser **Configure** panel, which holds them
in memory only. `render.yaml` sets `DEMO_MODE=true` for the hosted instance so it
runs with no keys. `.env` is gitignored and excluded from every archive; no API
key is committed anywhere (verified by scanning the packaged files for both key
values).

**Two items still need the author** — a real `docker build` (no daemon in the
dev sandbox) and the public repo + hosted URL (no deploy credentials here).
Exact commands for both are in `PROGRESS.md` § "NEEDS ME", and the full
requirement-by-requirement status is in the `PROGRESS.md` submission checklist.
