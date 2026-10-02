# VIDEO_SCRIPT.md — ≤ 3 minutes

**Target runtime: 2:45.** Recorded against the app in `DEMO_MODE` (instant
replay) with **one live call shown for real**, as explained below.

## Why the demo is mostly replayed — and why that is not cheating

Measured live latency, one page per document, NVIDIA `nemotron-3-nano-omni-30b-a3b-reasoning`:

| | measured |
|---|---|
| per document | **34 s – 88 s**, median **~51 s** |
| 3-document shipment, live | **~2.5 minutes** |
| Nebius summary call | **~6 s** |
| DEMO_MODE replay of the same shipment | **< 1 s** |

A 3-document live upload would consume ~85% of a 3-minute video on waiting. So
the script shows **one real live call** to prove the integration is genuine, and
replays the rest from the bundled cache of **real captured model output** — while
the app itself states on screen that it is replaying. The UI says *"model-backed,
replayed from cache — no API call made for this run"*; it never claims a live call
that did not happen.

**Record the live shot first.** NVIDIA returns intermittent
`503 ResourceExhausted` under load (one document failed five consecutive
attempts before succeeding on a later run), so capture it early and keep a
cached fallback ready.

---

## Shot list

### 0:00 – 0:18 · The problem, concretely

**Visual:** three PDFs side by side — invoice, packing list, bill of lading.
Zoom to the weight line on each.

**Narration:**
> "One shipment, three documents, three different people writing them down.
> The exporter's invoice, the forwarder's packing list, the carrier's bill of
> lading. They're all supposed to describe the same goods — and a single
> mismatched field becomes a customs hold, an entry amendment, or a penalty.
> Right now a human catches this by opening three PDFs and eyeballing them."

**Note:** do not show a naive diff yet; save it for the payoff at 0:55.

---

### 0:18 – 0:38 · What it does, and the two providers

**Visual:** the architecture diagram from the README, or a simple two-box
animation.

**Narration:**
> "This app reads the documents and cross-checks six shipment-level fields. Two
> providers, two jobs. **Document extraction** — the core pipeline — runs on
> NVIDIA's hosted API, `build.nvidia.com`, using the open-source model
> `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`. The **plain-English executive
> summary** runs on **Nebius Token Factory**, calling
> `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` through its OpenAI-compatible
> inference endpoint. Both are runtime API calls made by the running app."

**Required content point — both providers must be named here.** Do not cut this
section for time.

---

### 0:38 – 1:05 · One live call, unedited

**Visual:** screen recording. Click **Conflicts**. Start a visible timer or show
DevTools network tab so the wait is honest.

**Action:** run with `DEMO_MODE=false` and both keys set, on **one document
only**, so the wait is ~40–50 s rather than ~2.5 min. Speed-ramp the wait with a
"live API call" label — do not cut it out silently.

**Narration (over the wait):**
> "This is a real call, not a replay. The model is a reasoning model, so it
> thinks before it answers — that's where the latency comes from. It returns one
> JSON object per document, with every value copied **verbatim** as printed."

**Then show the parsed result and zoom the raw value:**

> "Notice it's returning `12,450.00 KGS` as a *string*. It never converts a unit
> and never does arithmetic. All of that happens later, in Python."

**Fallback if the live call 503s during recording:** use the cached path and say
so explicitly — "NVIDIA's shared workers are saturated right now, so I'm showing
the same response captured earlier" — then show the raw response body saved on
disk in `fixtures/live_responses/raw/`. That directory is real evidence and takes
ten seconds to show.

---

### 1:05 – 1:35 · The hard part isn't reading — it's judging

**Visual:** the comparison table, `shipment_messy`. Point at the gross weight row.

**Narration:**
> "Three documents, three different ways of writing the same weight:
> `12,450.00 KGS`, European-style `12.450,00 kg`, and `27,447.00 LBS`. A naive
> diff flags all three as conflicts. A human auditor would say 'that's fine,
> it's the same shipment.' So the app normalizes in deterministic Python —
> decimal conventions, units to kilograms, HS codes compared on their first six
> internationally-harmonized digits."

**Show:** the row reading **✓ consistent**, and the warning the normalizer emits
for genuinely ambiguous input.

> "And when it *can't* tell — like `1,200` which could be a thousands separator
> or a European decimal — it defaults and **says so**, rather than guessing
> silently."

---

### 1:35 – 2:05 · The centrepiece: a field that validates itself

**Visual:** `shipment_conflict`, container number row. Zoom the "Why" column.

**Narration:**
> "Here's the part I'd actually ship. A container number carries an ISO 6346
> mod-11 check digit — the field validates itself. Two documents disagree, and
> I don't need ground truth or a second model call to tell you which one is
> wrong."

**Read the on-screen text verbatim** (this is the real captured output):

> "The ISO 6346 check digit **arbitrates**: the packing list prints check digit
> 8 where the algorithm computes 2, so that value is internally inconsistent —
> probably a misread of one character. The bill of lading's number is the more
> likely correct one."

**Then the weight row:**

> "Gross weight differs by 5.68% — above the materiality threshold for a
> container shipment — and with three documents the app can name the *minority*
> report as the outlier, not just shrug at a pairwise diff."

**Beat.** This is the memorable moment; give it room.

---

### 2:05 – 2:25 · The Nebius summary

**Visual:** the Executive Summary card, with its "AI-GENERATED" tag visible.

**Narration:**
> "Finally, one optional call to **Nebius Token Factory** turns the verdicts into
> a sentence for a human. This is real model output —"

**Show, actual captured text:**

> **"4 fields agree and 0 fields need attention. No discrepancies need
> attention."**
>
> **"2 fields agree and 2 fields need attention. Gross weight and container
> number."**

**Then the important caveat, said out loud:**

> "It sits *alongside* the table. It's labelled AI-generated, and it can't change
> a verdict — every verdict above came from deterministic code. If this call
> fails, the shipment still reconciles normally."

---

### 2:25 – 2:45 · Evidence and export

**Visual:** click a value → evidence panel (verbatim quote, page number, page
image). Then download the ZIP and open `report.html` **from inside the archive**.

**Narration:**
> "Every value links to the line it came from and the page it was on — including
> a two-page document where the totals are genuinely on page two. The export is a
> JSON report, a CSV, and a standalone HTML report with its evidence images
> embedded, so it opens anywhere with no server."

**Close:**
> "The model reads. Python reasons. And when the two disagree about what's
> certain, the app tells you which is which."

---

## Honesty checklist — verify before exporting

- [ ] The live call is labelled live; the replayed parts are labelled replayed.
- [ ] The DEMO_MODE banner is visible on screen when replaying — don't crop it.
- [ ] Both provider names and both exact model IDs are spoken, not just shown.
- [ ] The no-OCR decision is framed as a **design choice**, not an omission.
      Suggested line if time allows: *"I deliberately didn't add a Tesseract
      fallback — the checksum catches misreads with certainty, which is strictly
      better than a second engine I'd then have to adjudicate between."*
- [ ] No claim that the judge's session will make live calls.
- [ ] If the 503 fallback is used, say so on camera rather than hiding it.

## Cut-first list (if over 3:00)

1. The architecture animation at 0:18 — replace with a single static frame.
2. The `1,200` ambiguity aside at 1:35.
3. Shorten the ZIP/export section to 10 seconds.

**Do not cut:** the two-provider naming (0:38) or the checksum arbitration
(1:35). Those are the compliance point and the actual idea.

## Recording practicalities

- 1500×1000 viewport renders the table without horizontal scroll; the UI was
  verified at that size.
- Pre-warm by loading each fixture once before recording so the cache is hot.
- Record the live call as a separate clip first, so a 503 late in the session
  doesn't force a full re-record.
