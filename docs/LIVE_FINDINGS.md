# Live findings

Measurements against real endpoints, each dated, with what it changed in the code. Anything here that names a date is a capture; anything without one predates the responses committed under `fixtures/`. A claim absent from this file was not measured.


## Extracting live

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

---


## Live findings that changed the code

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

---


## Measured, not remembered

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

---
