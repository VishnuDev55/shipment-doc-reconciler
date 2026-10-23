# Providers

Two OpenAI-compatible hosts are wired up. Both differ from the OpenAI spec in ways
that were established by calling them, not by reading their documentation — which
is why this file exists. If you add a third provider, the assumptions below are the
things to check first, with a real request, rather than to trust.

| | Vision (documents) | Summary / judgement |
|---|---|---|
| Default | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` on build.nvidia.com | `NVIDIA-Nemotron-3-…` on Nebius Token Factory |
| Base URL | `https://integrate.api.nvidia.com/v1/` | `https://api.tokenfactory.nebius.com/v1/` |
| Switch | `VISION_PROVIDER=nvidia\|nebius` | always Nebius |
| Model name | `NVIDIA_VISION_MODEL` / `NEBIUS_VISION_MODEL` | `NEBIUS_SUMMARY_MODEL` |

Either host can serve the vision role; `CONSENSUS=true` uses the other as a second
reader.

---

## Structured output: both hosts reject the schema their own docs show

Verified live on both. Sending the bare JSON Schema — the shape in NVIDIA's
example — fails:

- **NVIDIA:** `400` `missing field \`name\``
- **Nebius:** `422` `response_format.json_schema.name: Field required`

Both accept the standard envelope `{name, strict, schema}`. So `app/client.py`
sends the envelope, and a startup probe still decides per endpoint which of
`json_schema` (strict), `json_object`, or prompt-only the host actually honours —
because "accepts the request" and "obeys the schema" are different claims. The
probe's answer is recorded on every run as `run.json_schema_mode`.

The probe distinguishes **"unsupported"** from **"busy"**. A 400/403/422 means the
mode is not implemented; a 503/429/timeout means the endpoint is saturated, and the
probe retries rather than recording a verdict it never observed. Live, a 503 storm
once made the probe downgrade a model that had accepted strict schema minutes
earlier.

## Empty `content` is a normal success

Reasoning models put their chain of thought in `reasoning_content` (NVIDIA,
Nebius-Nano) or `reasoning` (Nebius) and can return `content: ""` or `None` on an
HTTP 200. Reading `.content` naively yields an empty extraction that parses as
"the document states nothing" — the worst kind of wrong answer, because it looks
like a result. `_message_text()` falls back to the reasoning field and the attempt
is stamped with which field the answer came from, so the report says so.

## Transient saturation on NVIDIA's shared workers

Live, verified: `ResourceExhausted: Worker local total request limit reached
(16/16)` cleared only on the fifth attempt. So the backoff ladders differ by
provider (`app/client.py:BACKOFF_SCHEDULE`):

```
nebius:  1s,  4s, 12s
nvidia:  2s, 5s, 10s, 20s, 30s
```

Retries happen **below** the parse ladder, so a 503 never burns a rung. A run that
spent its one correction call on a transport error would have wasted the only
attempt that could have fixed a malformed response.

### Cross-provider fallback

A document the configured host cannot answer is re-read on the other host, once,
per document (`VISION_FALLBACK`, default on; off in `DEMO_MODE`). It fires only
when the primary produced nothing usable — an exception or a null response —
never for a partial extraction, and only when the other host has a real key and
a closed breaker. Both attempts are kept: the document carries the merged
attempt and token counts, and the surviving answer is stamped with the host that
actually read it plus a routing note the UI and both reports render.

Live check, 2026-10-21 (`scripts/live_fallback_check.py`): with the NVIDIA base
URL pointed at a dead local port, the document still arrived — status `ok`,
`nebius / openbmb/MiniCPM-V-4_5`, note `nvidia returned nothing usable; read on
nebius instead`. No NVIDIA tokens were spent; the Nebius call is in the spend
log.

## Model discovery: NVIDIA publishes no modality at all

`GET /v1/models` on `integrate.api.nvidia.com` returns 81 rows carrying only
`id`, `object`, `created` and `owned_by`. There is **no modality field**, and
`/v1/models/{id}` returns the same four keys. Image capability on NVIDIA can
therefore only be *inferred* from the model name. `Selection.inferred` records that
fact, and a report can never present an inference as a host declaration.

Nebius does declare it, but not where you would look: with `?verbose=true` the
modality sits at `architecture.modality`. An earlier version of `parse_catalog`
read a field that does not exist, which made 0 of 25 Nebius models look undeclared
and turned discovery into a guess. It also invented `type` and `supported_content`
keys that no host returns — and the tests asserted those invented shapes, so the
suite was green against a payload nobody had ever seen. Both the code and the tests
were wrong together.

**Consequence for reports:** `run.model_selection` is one of `configured`
(confirmed present in the host's own catalog), `discovered` (the configured name is
not served, so a listed model was adopted), or `unverified` (no key, or the catalog
could not be read). A report should never imply a model was reachable when nothing
checked.

### Re-verified live on 2026-10-16 (`scripts/probe_live.py`)

Both providers answered with real, parseable responses (DEMO_MODE=false, keys from
`.env`):

- **Vision** — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` on
  `integrate.api.nvidia.com`. Structured-output mode **`strict`** (json_schema
  honoured). One fixture extraction parsed on the first attempt, 32.6 s wall clock,
  4,955 tokens. The capability probe first hit **`strict hit capacity (HTTP 503)`**
  twice and then `honours strict` — a live-only capacity flake the client's probe
  retry absorbed, not a config problem.
- **Summary** — `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` on
  `api.tokenfactory.nebius.com`. One call, 1,366 tokens, output passed the
  plausibility guard.

Nebius `/v1/models` now lists **28 models** (was 25 on 2026-10-02), of which 8
declare `text+image->text` modality. New vision-capable names present this date:
`nvidia/Nemotron-3.5-Super-VL` (a Nemotron **VL** — the first Nemotron vision
model on this host), `Qwen/Qwen3.8-27B`, `deepseek-ai/DeepSeek-V4.1-Flash`,
`moonshotai/Kimi-K2.6`, `moonshotai/Kimi-K3`, `meta-models/muse-spark-open`,
`zai-org/GLM-5.3-Flash`, plus the already-used `openbmb/MiniCPM-V-4_5`.

### Nemotron-3.5-Super-VL: the ceiling was ours, not the model's

The verbose catalog does publish this model's facts: `text+image->text`,
1,048,576 context, **$0.30 / $0.90 per million tokens** (input/output),
300 req/min, 2M tok/min, bf16. It was run live over the 12 fixture documents and
the 4 stress documents (twice each, `scripts/eval_model.py`) — first on
2026-10-20, then again on 2026-10-21 after a fix on our side.

**What the first run showed.** The probe **downgraded `strict` → `json_object`**
("strict accepted but returned unusable JSON"), so it never honours json_schema,
and **9 of 16 documents failed to parse**. The ladder's last error was
consistently a malformed body ("no balanced JSON object found" / "extracted first
balanced object; unrepaired"), and every failed document burned ~30k tokens
getting there.

**What was actually wrong.** Nothing in the model's JSON. This one reasons *in
`content`* — the chain of thought and the answer arrive in the same string — so
with `max_tokens=1600` the whole budget was spent thinking and the object never
started. The diagnostic that proved it (`runs/diag_supervl.py`) is one document,
three rungs, printing head and tail of each: rung 1 was 4,664 characters of pure
reasoning with no `{` anywhere in it, rung 2 wrote a real object that was cut off
mid-string, rung 3 reasoned again and was cut off. Every rung ended at *exactly*
the ceiling, which is the signature — a model that had decided to stop would have
ended below it.

`app/client.py` now checks that signature instead of blaming the parse: when a
response's `completion_tokens` equals the `max_tokens` it was asked for, that
rung is retried **once**, with the same messages, at 4,800 tokens (ceiling
8,192). It is one extra call only for models that reason in `content`, and it
changes no prompt, so no prompt version and no cache was invalidated. Live
afterwards: rung 1 still burns 1,600 tokens on reasoning, rung 2 returns a clean
object, status `ok`.

**Re-run, 2026-10-21 (`scripts/eval_model.py`, 16 documents × 2 runs):**

- **16 / 16 documents parsed**, 0 failures (was 7 / 16).
- **204 / 206 fields exact (99.0%)**, 0 invented, 112 / 112 absent-correct.
  The 2 misses are both on `stress phone_photo`, and both are misses rather than
  wrong claims.
- p50 **4.5 s**, p95 **21.2 s**, $0.0034 per document over 32 runs.

**Why it is still not the default.** It was tested against a written promotion
rule, on 2026-10-23, end to end: three demo shipments plus the 19-group planted
bundle, consensus off and on, each twice (`scripts/live_super_verify.py`, 200 live
calls). Every pass reached **3 / 3** shipment verdicts with **0** invented fields,
and every pass scored **74 / 75 (98.7%)** fields exact on the demo documents — the
same single miss reproduced four times out of four, where omni scores 75 / 75. The
rule required 3/3 verdicts, 0 invented **and exact ≥ 99%**, so it did not clear its
own bar and **omni stays the default**. `strict` json_schema is still rejected
(json_object only, so schema violations depend on the repair ladder), and the two
misses in the 2026-10-21 run land on the input type this app exists to handle.
It is selectable via `VISION_PROVIDER=nebius` +
`NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL`, the numbers above say what to
expect, and no prompt was tuned to it. What it actually buys is speed — p50
**10.0 s** through the engine against omni's measured **58.8 s** — not accuracy,
which is where it lost, and not money either: **$0.0034** per document against an
estimated **$0.002** for omni makes it ~1.7× *more* expensive per document, while
still far below the cost of a human minute.

## Documents are rasterised, not uploaded

Token Factory's Files endpoint is batch-JSONL only — there is no document-upload
path into chat completions. So PDFs are rasterised locally with `pypdfium2` (pure
wheels, no poppler) and sent as image data URIs. That is also what makes the cache
work: the key hashes the canonical JPEGs, so a re-encoded or resized upload of the
same page still hits.

---

## What each provider actually got right, measured

`docs/EVAL.md` is generated from the captured responses in `fixtures/live_responses/`
— real model output, both hosts, twelve synthetic documents, ten fields.

| | NVIDIA | Nebius (MiniCPM-V) |
|---|---|---|
| Field observations | 75 | 75 |
| Exact on `compare_key` | **75 / 75 (100 %)** | **75 / 75 (100 %)** |
| Verbatim as printed | 74 / 75 | 74 / 75 |
| Printed but missed | 0 | 0 |
| **Invented** (not on the document) | **0** | **6** |
| Shipment verdicts reached correctly | 3 / 3 | 1 / 3 |

The invented values are the finding. Nebius reported `po_number` on 3 documents and
`country_of_origin` on 3 more where nothing is printed — and because the engine
trusts `present`, those fabrications make the *clean* fixture read as `conflicts`.
That is the failure mode the architecture cannot defend against: a missed field is
shown as absent, but an invented one manufactures a disagreement between documents
that agree.

Prompt tightening did not change the output. The behaviour was re-measured after
sharpening the surface hints for `po_number` and `country_of_origin`, and MiniCPM-V
returned the same fabrications, so the hints were kept (they are correct on their
own terms) and the limit is recorded here instead of being smoothed over.

`exact` at 100 % on both hosts is a statement about the normalizers, not about the
models: `13,200.000 KGS` for a page printing `13,200.00 KGS` counts as exact because
the deterministic layer absorbs it and no verdict moves. That is the point of the
architecture, and the number is only meaningful because the scoring is done on
`compare_key`.

**The limits of that table, in its own words:** twelve documents, one domain, and
the same author wrote the questions and the answers. Nothing there measures
performance on a scan of somebody's real packing list.

---

## Live cost and latency, measured

A single-document call through the shipped pipeline on 2026-10-02
(`fixtures/images/invoice_conflict.png`, NVIDIA, probe chose `strict`):

| | |
|---|---|
| Result | `status: ok`, `source: live_api`, parsed on ladder rung 1 |
| Tokens | 3,145 prompt + 1,057 completion = **4,202** |
| Model latency | **35.1 s**; ~190 s wall including the probe |
| Raw body | 965 bytes written to `runs/{id}/raw/doc1_initial.txt` |

Set `USD_PER_MTOK` to price a run in the transparency panel. It defaults to 0, and
at 0 the panel declines to show a cost at all rather than guess one: both hosts
meter differently and change prices without notice, and a stale rate on an audit
screen is worse than no rate.

## Re-verifying

```bash
make probe-live      # both hosts: discovery, extraction, summary — real calls, real spend
make probe-diff      # live output diffed against the captured cache, no calls
```

`scripts/live_compare.py` refuses to label a non-live outcome as live and exits
non-zero when a provider returns anything but a real API response; `--seed-cache`
copies captures in afterwards. That guard exists because an earlier version of the
script wrote hand-authored replays into the cache marked "LIVE API response" and
printed a count of live calls that had not happened. Nine mislabelled files were
deleted before anyone committed them.


## Why two providers rather than one

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

---


## Live facts, measured not assumed

Pricing, context windows, rate limits and latency are not in either provider's
docs in a usable form. Each number below came from calling the host with a key,
and each carries the date it was read, because both catalogs move.

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

---


### NVIDIA build.nvidia.com

`GET /v1/models` returns 82 models. **No rate-limit, quota or credit headers are
returned at all** — spend visibility is dashboard-only, so live calls were kept
minimal. Measured behaviour:

| Observation | Value |
|---|---|
| Structured output | `json_schema` accepted (wrapped envelope) |
| Vision latency | three separate measurements, not one number — see below |
| Availability | intermittent `503 ResourceExhausted: Worker limit (16/16)` |
| Reasoning | returned in `reasoning_content` alongside `content` |

**Three latency figures, each labelled with what it measured.** `30–50 s per
page` was a single-page sample on 2026-10-02; `35.1 s` is one document's model
latency in the table under **Live cost and latency, measured**; and the
twelve-document live run behind `docs/EVAL.md` puts omni at **p50 58.8 s /
p95 67.2 s**. They are not in conflict — the later one is a wall-clock
distribution over a whole run, the earlier ones are individual calls — but
quoting any one of them as "the" latency would be wrong, which is why this file
quotes all three with their dates. Reasoning VLM latency is also the reason the
demo pre-warms or replays from cache rather than waiting.

Measured on a real 1-page extraction: **1,926 prompt + 404 completion tokens =
$0.0017 per document**, so a 3-document shipment runs about half a cent. The
endpoint also reported 1,920 of those prompt tokens as cached.

---


## Model discovery: the rules the app runs by

Model names are treated as defaults, not facts. Before a run extracts, the app
issues `GET /v1/models` on each host (with `?verbose=true` on Nebius, which is
where the modality comes from) and checks that the configured model is actually
served. If it is not, the app adopts one that is — preferring the Nemotron
family, then any other model that takes image input — and says so in the
transparency panel rather than failing every document silently.

This exists because the assumption already cost the project: the brief specified
`nvidia/Nemotron-Nano-12B-V2-VL`, which returned **410 Gone** on
build.nvidia.com and **404** on Token Factory, and that was found by hand, with a
live key, mid-build. What each host actually publishes — including NVIDIA
returning no modality at all, which is why `Selection.inferred` exists — is the
section above, **Model discovery: NVIDIA publishes no modality at all**, and the
2026-10-16 re-verification under it. Both sides have been checked against a real
host; `make probe-live` re-checks them.

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

---


## Optional: run vision on Nebius instead

```bash
VISION_PROVIDER=nebius                                          # MiniCPM-V by default
NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL                # or the Nemotron VL
```

Extraction on Nebius is not a fallback you have to take on faith: both models are
live-verified over the same twelve demo documents, and the replay cache holds a
captured answer from each, so either path can be demonstrated with no key.

| Nebius vision model | Exact on `compare_key` | Invented | Shipment verdicts | p50 per document |
|---|---|---|---|---|
| `openbmb/MiniCPM-V-4_5` | **75 / 75** | **4** | **2 / 3** | ~6 s |
| `nvidia/Nemotron-3.5-Super-VL` | **74 / 75 (98.7%)** | **0** | **3 / 3** | **10.0 s** (p95 22.1 s) |
| default: omni on NVIDIA | 75 / 75 | 0 | 3 / 3 | 58.8 s (p95 67.2 s) |

Read the middle two rows together, because they say different things. MiniCPM-V
is exact on every value it was asked to read and still reaches only 2 of 3
verdicts: the four fields it asserted that no document prints manufacture
disagreements the paperwork does not contain, which is the one failure the
architecture cannot defend against. Super-VL invents nothing and lands the three
verdicts, but misses one field exact — a `po_number` printed `PO 4410-DE` read
back as `PO 410-DE` — reproducibly, in all four live passes.

`nvidia/Nemotron-3.5-Super-VL` was tested against a written promotion rule
(3/3 verdicts, 0 invented, exact ≥ 99%) on 2026-10-23 and cleared two of three
conditions, so **omni stays the default** and Super-VL stays selectable; no
"Nebius-only" preset was added. Its dollars are real (`$0.0034` per document on
the catalog rate) while omni's are an estimate, because NVIDIA publishes no
per-token price. The older claim in this file's history — that MiniCPM-V was the
*only* `text+image→text` model on the key — is out of date: the 2026-10-21
catalog lists eight. See `docs/EVAL.md` §Vision model comparison for the table
this section is summarising; it is generated from `fixtures/model_comparison.json`.

---
