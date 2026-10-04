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
