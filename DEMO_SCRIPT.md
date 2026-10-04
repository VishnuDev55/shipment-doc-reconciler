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

Upload a real document. Expect 30–90 seconds per document — it is a reasoning
vision model, and the progress line says so. `run.json_schema_mode` will show what
the capability probe decided about that endpoint, which is decided by asking, not
by assumption.

Then `make probe-live` to verify both hosts, and `python scripts/evaluate.py` to
regenerate `docs/EVAL.md`.

## Questions you will be asked, and the honest answers

**"How accurate is it?"** On our twelve synthetic documents, both providers were
exact on the comparison key for 75 of 75 field observations. That is a statement
about the normalizers absorbing transcription variance, not about the models being
right. `docs/EVAL.md` has the table and its limits.

**"What if the model makes something up?"** It happens, and we measured it: one
provider reported six field values that no document prints, and those flipped a
clean shipment into conflicts. That is the failure the architecture cannot defend
against — a missed field is shown as absent, but an invented one manufactures a
disagreement. Tightening the prompt did not change it. This is the single most
important known limitation and it is in the README, not buried.

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

**"Is it production-ready?"** The hard part is. What is not verified: CI has never
run (no remote is configured), no hosted deployment has been made from this
machine, and the goods-line feature has never been run against a live model. Those
three are stated in `docs/DEPLOYMENT.md` and `README.md` rather than glossed.

**"What breaks first under real load?"** Filesystem runs with no cross-run queries,
and a single process holding reports in memory. `MAX_REPORTS_IN_MEMORY` bounds the
memory; the storage model is a real constraint and `docs/adr/0004` records why it
was accepted anyway.
