# Demo script

Ten minutes, no key, offline. Every claim below is checkable on the machine
running it — the point of the demo is that the tool shows its evidence, so the
script should show the same.

## Before you start

```bash
DEMO_MODE=true python -m uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000`. The header chip reads **demo mode — serving from
cache**. Leave it visible; it is the honest label for everything that follows.

## 1. The problem, in 45 seconds

Show the intake panel. Three documents for one shipment: a commercial invoice, a
packing list, a bill of lading. A clerk reconciles them by eye, and the failure is
not dramatic — it is `12,450.00 KGS` against `12.450,00 kg` against `27,447.00 LBS`
being the same weight, and nobody knowing that until the container is on a ship.

## 2. The clean shipment

Click **Clean**. Point at the header: all fields agree.

Then click any value's **page n →** chip. The page opens with the printed line
quoted beside it. This is the whole argument of the tool in one interaction: a
verdict and the sentence it came from, together.

## 3. Where the intelligence actually is

Click **Conflict**. Walk the table top to bottom and narrate what the engine did,
not what it printed:

- `12,450.00 KGS`, `12.450,00 kg` and `27,447.00 LBS` — **consistent**. Three
  conventions, one weight. Say out loud that no model did this arithmetic.
- `1,200 pieces` against `100 cartons` — **suspicious, not a conflict**. One counts
  items, the other counts packaging. This is the difference between a tool people
  use and one people switch off.
- The container number — if two disagree, the ISO 6346 check digit arbitrates. The
  number is self-verifying, so a single misread digit is detectable with no ground
  truth and no second call.
- The HS code — six digits are international, digits 7–10 are national, so the
  comparison uses six and the display keeps ten.

## 4. The thing that cannot be faked

Open **Transparency**. For each document it names the model, the provider, the
source and the token cost, and the run stamps `live_calls: 0`.

Say plainly: *this run made no API call. These values are real model output,
captured earlier and replayed.* Then say which parts of the demo are hand-authored
fixtures rather than model output. The rail already says it; reading it out is the
difference between a demo that oversells and one that does not.

## 5. A human overrides it

Correct a value in the UI — type over a weight. The comparison re-runs immediately,
with no new API call, and the row is marked **corrected by … · model said …**. The
model's original answer is still on file.

Then open the corrections list and revert it. Mention that any AI-written summary
generated against the old values is now stamped superseded rather than quietly
reused.

## 6. Take it away

Download the ZIP (JSON + CSV + standalone HTML) and the PDF. The PDF repeats the
provenance and the version stamps, so a printed report cannot be read as current
when it is not.

## Optional, if you have a key

```bash
DEMO_MODE=false NVIDIA_API_KEY=*** python -m uvicorn app.main:app --port 8000
```

Upload a real document. On the default configuration expect about a minute per
document — measured live on 2026-10-23 across the twelve demo documents, NVIDIA
omni (the default vision model) is p50 **58.8 s** / p95 **67.2 s** per document.
The two Nebius vision models are much faster: `nvidia/Nemotron-3.5-Super-VL`
measured p50 **10.0 s** / p95 **22.1 s**, and `openbmb/MiniCPM-V-4_5` about
**6 s**. `run.json_schema_mode` will show what the capability probe decided about
that endpoint, which is decided by asking, not by assumption.

Then `make probe-live` to verify both hosts, and `python scripts/evaluate.py` to
regenerate `docs/EVAL.md`.

### Which provider does what, said out loud

Extraction — the core function, all ten fields — runs on **build.nvidia.com** by
default. Nebius Token Factory does the executive summary
(`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`), the optional consensus reader, the
optional advisor, and is the vision fallback. Extraction can also be moved to
Nebius with `VISION_PROVIDER=nebius`; that path is live-verified on MiniCPM-V and
on `nvidia/Nemotron-3.5-Super-VL`.

### Replaying captured Super-VL output in demo mode

The replay cache now carries live-captured `Nemotron-3.5-Super-VL` reads of the
three demo shipments (captured 2026-10-23, stamped `live API response`). Point
demo mode at them and the same keyless demo runs on Nebius vision instead:

```bash
DEMO_MODE=true VISION_PROVIDER=nebius \
NEBIUS_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL \
python -m uvicorn app.main:app --port 8000
```

Still zero API calls — the transparency panel keeps saying `live_calls: 0`,
because these are recorded reads, not calls made now.

## Questions you will be asked, and the honest answers

**"How accurate is it?"** Measured live, against ground truth we wrote: on the
twelve bundled synthetic documents NVIDIA omni is exact on the comparison key for
**75 / 75** field observations (**74 / 75** verbatim) with **0** invented values,
and reached the right verdict on **3 / 3** shipments. Nebius MiniCPM-V is also
**75 / 75** exact but invented **4** values, and reached **2 / 3** verdicts.
Nebius `Nemotron-3.5-Super-VL` scored **74 / 75 (98.7%)** exact with **0**
invented and **3 / 3** verdicts, at p50 **10 s** where omni is **59 s**. Those
documents are synthetic, and the same author wrote the pages and the answers — so
the gap they measure is between models, not between the tool and reality.
`docs/EVAL.md` has the tables and states their limits.

**"What if the model makes something up?"** It happens, and we measured it: the
Nebius vision captures reported **4** field values no document prints (a PO number
that is really the invoice reference, a country of origin lifted from "Ningbo,
China" on a bill of lading), and that escalated the *messy* shipment from the
expected `review` to `conflicts` — 2 of 3 verdicts instead of 3 of 3. The same
provider is still 75/75 exact on every value it was asked to read. That is the
failure the architecture cannot defend against — a missed field is shown as absent,
but an invented one manufactures a disagreement. Tightening the prompt did not
change it. This is the single most important known limitation and it is in the
README, not buried.

**"Does it work on German, French, or a bad phone photo?"** Measured, on the
harsh-input cases `scripts/run_stress.py` generates: the Nebius MiniCPM-V reader
took the German invoice **10 / 10** fields and the French invoice **10 / 10**,
zero invented on both (2026-10-21), and on the rotated, sheared, darkened photo the
same model went from **6 / 10 with one invented value to 8 / 10 with none** once
orientation preprocessing was on. Say plainly what that does *not* buy: **every one
of those pages is synthetic** — we generated the French `FACTURE COMMERCIALE` and
the German case ourselves, and know their exact printed strings, which is what
makes them scorable. And **no omni number exists for either case**: the default
NVIDIA model hung past the 150 s deadline on the German page and on both photo
variants, so the honest statement is "not measured", not "worse" or "better".

**"Why not just ask the model which documents disagree?"** Because then the answer
is not reproducible, cannot be argued with, and a misread digit becomes a confident
wrong verdict. `docs/adr/0001`.

**"Is the fuzzy name matching the same thing as the line matching?"** No, and
deliberately not. Party names get similarity bands because legal-entity variance is
unbounded. Goods lines get exact matching because `H-40` and `H-41` are two parts,
and a band would merge them and hide the only real error. `docs/adr/0006`.

**"Does it know customs law?"** No. It reconciles documents and computes a risk
score from its own deterministic verdicts. It does not validate a tariff code
against a schedule, does not convert currency, and does not classify goods.

**"Is it production-ready?"** The hard part is. CI now really has run on GitHub —
and the first run went red on a dependency pin before a green one followed
(`docs/DEPLOYMENT.md` has both run numbers), so say that out loud rather than
claiming a badge. What is still not verified: no hosted deployment has been made
from this machine, and the goods-line feature has never been run against a live
model. Those are stated in `docs/DEPLOYMENT.md` and `README.md` rather than
glossed.

**"What breaks first under real load?"** Filesystem runs with no cross-run queries,
and a single process holding reports in memory. `MAX_REPORTS_IN_MEMORY` bounds the
memory; the storage model is a real constraint and `docs/adr/0004` records why it
was accepted anyway.
