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

**Why it is still not the default.** `strict` json_schema is rejected (json_object
only, so schema violations depend on the repair ladder), the two misses land on
the input type this app exists to handle, and no three-shipment verdict check has
ever been run for it — the verdict column in `docs/EVAL.md` is `--` for that row.
It is selectable via `VISION_PROVIDER=nebius` +
`NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL`, the numbers above say what to
expect, and no prompt was tuned to it. The default remains
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` on NVIDIA.

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
