# Features, one section each

Every capability, how it decides, and what it deliberately refuses to do. This is the detail the README used to carry; the front door links here.

The shape repeated throughout: a model transcribes, Python decides. Each section says where that line sits.


## Configuration: two keys, then the drawer

Live mode needs **two keys from two different places**, because the two roles run
on two hosts:

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
VISION_PROVIDER=nvidia               # nvidia (default) | nebius (MiniCPM-V or Super-VL)
DEMO_MODE=false                      # false = make real calls
VISION_FALLBACK=true                 # re-read a failed document on the other host
ENABLE_SUMMARY=true
```

`.env` is gitignored and excluded from every archive; only `.env.example` is
committed, and it contains placeholders only. Verify both providers before trusting
anything else on the screen:

```bash
make probe-live     # one real extraction and one real summary, printing usage and any error body
make probe-diff     # live extractions diffed against the authored cache, no calls
```

`scripts/probe_live.py` is the only thing that touches the network during
verification: it runs the capability probe on each endpoint, performs one real
vision extraction, and generates one real summary from a fixture report — printing
the actual response, the token usage and any error body.

Keys can also be applied at runtime. **Configure** in the header opens a drawer that
changes settings on the running process — vision provider, both keys, the summary
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
  does exactly that, which is why a deployed instance is env-driven rather than
  drawer-driven.
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

---


## Transparency panel

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

---


## Progress: pushed, with a poll behind it

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

**Feedback from the click, not from the first event.** The button disables, names
itself "Uploading…" and shows a spinner in the click's own frame; the browser
suite measures that from the click event's own `timeStamp` (2.6 ms on a
development box, and the test allows 100 ms). Every chosen file carries its own
state -- queued, uploading, uploaded, then whatever stage the stream reports for
it (reading, verifying, comparing, done, failed) -- beside a combined bar fed by
XHR upload bytes. One multipart request reports bytes for the whole body, not per
part, so cumulative file sizes are what turn the measurement into "this file is
the one being sent now" instead of a figure the browser never gave. A single
plain line names the work ("Reading packing_list.png with
nemotron-3-nano-omni-30b-a3b-reasoning on NVIDIA… 23 s"), and only the document
being read pulses.

**Two loops, and both can be switched off.** The pulsing dot and the upload
shimmer are the only animations that repeat; everything else is a 150-250 ms
ease-out transition. `prefers-reduced-motion: reduce` turns the loops off rather
than letting them run once, which would be a flicker. The bar is a real
`role="progressbar"` with `aria-valuenow`, and the two status lines are
`role="status" aria-live="polite"`, so a screen reader hears what changed rather
than every frame.

**A failure stops the animation.** A timeout, a dropped network or the daily
cap's 429 ends the bar, marks the files failed, restores the button, and shows
the server's own message -- the cap's `detail` is the one that names the number
and the reset time -- with a **Retry** that takes focus. Nothing is left
spinning: when a run settles the upload bar hides, the stage line clears, and a
file row the stage stream never reached is settled from the report itself.

**A waking server says so.** `/api/health` is pinged on load. After four seconds
without an answer the page says "The server is waking up (free plan). This can
take up to a minute the first time." and the connection dot turns amber; when the
answer arrives the note disappears and the dot reads "Server ready". A free
Render instance spins down after 15 idle minutes, so this is the common first
impression rather than an edge case.

---


## Cross-field sanity

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

---


## Risk score: 0–100, with the arithmetic shown

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

---


## Evidence you can navigate

Every value in the comparison table carries a **page N →** chip. It opens that
document's page image with the exact line the model quoted as evidence beneath it,
so a verdict can be checked against the source without hunting through uploads.
Escape, the **×** button, or a click on the dimmed area closes it, and focus goes
back to the chip or thumbnail that opened it.

**Reading it closely.** The page opens fitted whole; a toolbar zooms in and out,
and so do the mouse wheel, a trackpad pinch, a two-finger pinch on a touch screen
and **+** / **−** / **0** on the keyboard. Once zoomed, dragging pans. The image
and its rectangle are a single transformed box, so the highlight stays on the line
it was drawn on at every zoom and every pan. Clicking the image deliberately does
not close the overlay -- the whole overlay used to be one close button, so the
natural way to look closer at a page dismissed it -- and the dark area still does.

**The line is drawn on the page, when the page can be asked where it is.** The
coordinates do not come from the model — the extraction contract still carries
`page_index`, `evidence_text` and no box, and adding one would be a prompt
change, which invalidates every captured response in the cache because
`PROMPT_VERSION` is part of its key
([NORMALIZATION.md](NORMALIZATION.md) §Changing a rule). They
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

---


## The quote is checked against the page

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

---


## Arithmetic inside a single document

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

---


## One severity per finding, and the banner that follows from it

Six panels can now comment on one shipment: the verdict, the field table, the
cross-field rules, the arithmetic, the goods lines, and the evidence checks. Each
answers its own question, and a reviewer was answering theirs by reading all six.
`app/severity.py` gives them one answer, by looking at every finding the other
stages already produced and assigning each a level from one table.

| Level | What earns it |
|---|---|
| **CRITICAL** | container number, consignee, HS code (first six digits), country of origin, or an invoice value stated in two currencies |
| **WARNING** | any other cross-document conflict, a suspicious reading, a value missing from a document type that normally prints it, a document that could not be read, an internal arithmetic failure, a withdrawn unverified value, a cross-field rule at `risk`/`warn` |
| **INFO** | a difference of counting basis (items against cartons, whatever the two numbers are), the same count printed with different package words, a field only one document stated, a field no uploaded document was expected to carry, a check that did not run, a goods line one document printed alone |

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

**Absence is two findings, not one.** A field nobody printed splits on whether a
document that normally prints it was in the run (`carrier_uploaded()` in
`app/compare/__init__.py`, over the same `EXPECTED` map the comparator uses). If
one was, the value is missing from paperwork that should have carried it:
**WARNING**, `{field} stated on no document`. If none was, nobody was ever going
to state it: **INFO**, `{field} not checked`, worded as an instruction to upload
the invoice rather than as a defect. Live-tested: a set with no invoice in it
reported "HS code stated on no document" as a warning *and* 55 risk points — the
high band on its own — while the comparison row beside it said the absence was
expected. Both readers now ask the same question first, and the risk strip scores
`hs_code_absent` only when a carrier was uploaded. An invoice that was uploaded
and could not be read still counts as a carrier: that is a coverage gap, and the
`document:failed` warning beside it says so.

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

**One instruction per report.** The banner is the only line allowed to tell the
reader what to do, because it is the only line that has read every stage. The
verdict headline describes the field table and nothing else. Live-tested: the web
report printed "Conflicts found — reconcile before the goods move" directly above a
banner reading "Review needed" — two orders, and no way to tell which governs. The
headlines are now descriptive ("Conflicts found in the field table") and word
identical between `app/export.py` and `web/app.js`, pinned by a test that parses the
JS object and compares it to the Python dict rather than trusting either one. Where
the two panels can legitimately differ — the banner is stricter, because a stage the
comparison cannot see found something — `StatusReport.bridge` carries one sentence
saying why, and it renders in the web app, in `report.html` and in the PDF. It is
empty whenever the banner and the verdict already agree, so a matching report adds no
text. Four pairs need it: review beside conflicts, review beside clean, and blocked
beside each of those two; every combination of severity counts, verdict and risk band
is enumerated in `tests/test_severity.py`.

**What READY TO SHIP does not mean.** It means no rule found something to block.
It is not a guarantee that every value was cross-checked: on a shipment where only
one document states the invoice value, nobody compared it, and that is exactly the
case where an invented number survives. A phone photo of a printed invoice has no
text layer, so the evidence check in the section above cannot run on it at all --
that shipment can read `ready` with a value on it that no document prints. The
banner's own caveat line says this in the same words, and the informational count
beside it is how you can see how much of the report was never compared.

---


## Scored against ground truth the bundle declares in advance

`fixtures/planted.py` holds 20 synthetic shipments whose right answer is written
down before anything runs: 3 clean, 6 tricky-but-correct, 10 with **exactly one
planted error**, and 1 case the engine cannot catch. They read 45 document slots
off 22 distinct pages -- identical paperwork is rendered once, because a cache key
is a hash of the page images and one page can only carry one authored answer. `scripts/evaluate_planted.py`
runs all of them through the real pipeline and prints what it found:

```
planted errors caught   10/10
false alarms            0/9  (clean + tricky shipments not called ready)
designed miss           1 declared
wall clock              2.9s for 20 shipments (45 documents)
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
[`docs/PLANTED.md`](PLANTED.md), which carries the per-case table and the
reasons.

**The miss is part of the score, not a footnote to it.** `miss_inside_tolerance`
plants a 600 g gap on a 12,450 kg shipment. The engine's absolute band is 1 kg —
chosen because two re-weighs of a container rarely agree to the gram — so the
report says the paperwork agrees, and it is wrong about the cargo. That is the
price of a tolerance, and a bundle with no miss in it is measuring its own code.

Everything here is authored, including the model responses, so the run is
reproducible and free. It says nothing about live accuracy; `docs/EVAL.md`
measures that from captured responses, and the two must not be confused.

---


## Correction drafts, written by rule

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

---


## Corrections a human can make, and undo

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

---


## Two readers, when you can afford the doubt

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
captured **omni** response *and* captured **Nebius** responses (MiniCPM-V and
Super-VL) for all twelve fixture documents, so consensus can be demonstrated
offline against genuine transcriptions from both hosts rather than invented ones.

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

---


## Line by line, when the totals agree and the shipment is still wrong

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

**Verification status, measured rather than assumed.** The goods-line read has
been run against live models: 80 billed reads are in the run records under
`runs/`, 75 on Nemotron-3.5-Super-VL and the rest on the omni reasoning model. No
fixture page has goods lines captured into `fixtures/responses`, so `DEMO_MODE`
reports `skipped` rather than showing a table it did not read. On these pages
only the invoice prints a goods table, so cross-document matching had nothing to
match in 74 of the 80 — the arithmetic checks still ran, because they read one
document at a time.

**What the second call costs**, taken from that recorded spend rather than
estimated, and making no new calls:

| Model | Extra tokens | Extra cost | Extra wall clock |
|---|---|---|---|
| Nemotron-3.5-Super-VL | ~2,475 per document; line items total +52% on top of the same shipment's scalar reads | ~$0.0013 per document | median 2.3 s, p75 6.7 s for the shipment |
| omni (the model `render.yaml` deploys) | ~23,700 per document, about 4× its ~6,000-token scalar read | ~$0.0033 per document | 50–110 s per document |

Off by default, and left off in `render.yaml` for that reason: on the omni model
the extra read roughly quadruples per-document tokens and adds a minute per
document, which would cut the $2 cap's capacity from about a thousand documents
to about four hundred. On Super-VL the same read is cheap enough to be worth
merging `ENABLE_LINE_ITEMS=true` in.

---


## The adjudicator: a second opinion, never a second decision

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

---


## Export

`Download reconciliation ZIP` produces exactly what `app/export.py:build_zip`
writes — checked against that function, not against memory of it:

```
report.json    full machine-readable report, with evidence and version tags
report.csv     one row per field per document — pivots cleanly in a spreadsheet
report.html    standalone and self-contained: inline CSS, evidence images
               embedded as data URIs. No CDN, no network. Opens straight out of
               the zip.
report.pdf     the same report laid out for print and filing, also served on
               its own at /api/shipments/{id}/report.pdf
corrections/   one .txt per drafted correction, written by rule with no model
README.txt     the manifest: what each of the above is, and how to read them
pages/         the normalized page images used as evidence
raw/           every raw model response, including failed attempts
```

`report.pdf` is the one conditional member: if rendering fails, the manifest says
so in the archive rather than leaving the reader to notice an absent file.
`pages/` and `raw/` are copied from the run directory when it still exists —
which is the point of the storage design: the directory *is* the artifact and the
zip is its compression.

`report.json` records `prompt_version`, `normalizer_version`,
`comparator_version` and `model`, so a cached response can never be silently
reconciled against a newer, incompatible ruleset.

---
