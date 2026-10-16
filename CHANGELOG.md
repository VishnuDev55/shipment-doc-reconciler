# Changelog

All notable changes, grouped by the phase that produced them. Every entry is a
commit in `git log`; the phase boundaries are the audit's prioritized plan, not
calendar weeks.

The format follows Keep a Changelog's grouping, but the entries are written as what
changed and what it cost, because that is the useful history for this repository.

## Unreleased — evidence, severity and release hygiene (2026-10-10 → 2026-10-16)

Six features that make the report defensible rather than merely informative, then
the hygiene pass around shipping them. None of them ask a model to decide
anything: the new stages read facts the deterministic engine already produced.

### The engine

- **Evidence verification** (`app/textlayer.py`, `app/verify.py`): the PDF's own
  text layer is walked character by character to recover where each printed line
  sits, and every value the model quoted is checked against it. A quote that is
  not on the page withdraws the value from the comparison instead of trusting it
  (`report.unverified`, severity `evidence:excluded`). Costs about 6 ms per 1,800
  characters and needs no extra model call. The limit is stated rather than
  papered over: a phone photo of a printed page has no text layer, so nothing on
  it can be checked at all.
- **Highlight on the page** (boxes from `app/textlayer.py` → `web/app.js`
  lightbox): when the quoted line's position is recoverable, the evidence viewer
  draws it. Only the exact-line tier yields a box — a fuzzy containment match
  would draw a rectangle that is not the line, which is worse than drawing
  nothing, so the tier check is load-bearing and pinned by a test that reaches
  the fuzzy path on purpose.
- **Internal arithmetic** (`app/mathcheck.py`): four rules that check a document
  against itself. `not_run` is a state and is reported as information, so an
  empty findings list cannot be read as "the paperwork adds up". Three of the
  four need `ENABLE_LINE_ITEMS=true`.
- **One severity per finding** (`app/severity.py`): six panels used to comment on
  one shipment and the reviewer reconciled them by reading all six. A single
  `SEVERITY_TABLE` now decides every level and the banner follows mechanically
  from it. It may be stricter than the verdict and never kinder; both directions
  are pinned, and the risk-score backstop was measured never to fire on the
  bundled fixtures — the table already covers every point the score awards.
- **Correction drafts written by rule** (`app/correction_email.py`): generated
  from the findings the engine already produced, with no model in the path, so
  nothing can be added, softened or invented between the verdict and the message.
  Only critical and warning findings are written up; an informational item is not
  a request to change anything. On a 1-1 tie both parties are asked rather than
  one being accused at random, and a container number that fails its own ISO 6346
  check digit settles which paper is wrong — recomputed from the normalizer rather
  than parsed out of the prose reason, because the prose is ours to reword.

### Measuring it

- **Planted-error bundle** (`fixtures/planted.py`, `scripts/evaluate_planted.py`,
  `docs/PLANTED.md`): 19 cases with the expected outcome declared in advance and
  scored against ground truth, on authored responses only — zero API calls,
  $0.00. 9 of 9 planted errors caught, 0 of 9 traps raised a false alarm, and
  the one miss (a weight inside tolerance) is declared in the bundle rather than
  discovered by it. Pages are deduplicated by content hash, 8.8 MB → 3.8 MB,
  which also removed a cache collision where one page set had two authored
  responses behind it.

### Release hygiene

- `country_of_origin` conflict is now **critical**, not warning. A judgment call,
  and recorded as one: origin drives the duty rate and whether an FTA preference
  applies at all, and the risk strip already scored it at 60 points — above the
  high band at 55 — while the severity table called it survivable. Two readers of
  one report disagreed about how bad the same fact was. Measured: reverting the
  row breaks 2 tests.
- **CI had never run.** The workflow triggered on push to `main` in a repository
  whose default branch is `master`. It now triggers on both. The mypy step prints
  its count against the 39-error baseline instead of only reporting it, and warns
  when the number grows; the stale "36 errors in 9 files" figure was corrected
  here and in `CONTRIBUTING.md`, while `CHANGELOG.md` keeps its historical 36
  because that was the measured number when it was written.
- README carries a CI badge placeholder. It is deliberately static: with no
  remote, a live badge renders "no status", which reads as green to anyone
  skimming. The one-line swap is commented directly above it.
- Two bugs found while doing this, both pre-existing and both in the export ZIP:
  `store.write()` used the platform default encoding, so a `report.json`
  containing a non-ASCII character was written as cp1252 on Windows and could not
  be read back as UTF-8 (now pinned to UTF-8 on write and on both reads); and the
  `README.txt` manifest had a ternary whose precedence dropped the arithmetic and
  unverified lines whenever `report.unverified` was empty.



### Phase 5 — documentation

- `docs/ARCHITECTURE.md`: the shape of the system, the seven invariants with where
  each is enforced, the module map, and why the frontend is still one file.
- `docs/NORMALIZATION.md`: every rule and threshold, with the edge cases that came
  from observed model output rather than from theory.
- `docs/PROVIDERS.md`: what each host actually does, measured by calling it, and
  what is still unverified.
- `docs/SECURITY.md`: the threat model, including the four things not protected.
- `docs/adr/`: nine decision records, including two decisions that were reversed by
  measurement while being made.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `DEMO_SCRIPT.md`.

### Phase 4 — hardening, packaging and the browser

- **Goods-line matching** (`app/lineitems.py`, opt-in via `ENABLE_LINE_ITEMS=false`)
  so a shipment whose totals all agree can still be reported as wrong on a line.
  Exact matching, never fuzzy. Three defects found and fixed while building it: a
  document credited to only its first row, a cache-fallback collision that would
  have parsed cleanly into an all-absent report, and a claim about documents nobody
  read. `docs/adr/0006`.
- **Dependency packaging**: `requirements.txt` pinned and split runtime/dev,
  `requirements.lock.txt` as the reproducibility record, `pyproject.toml` with ruff,
  mypy and coverage configured from measured baselines (ruff 92 findings resolved by
  scoping, no source rewritten; mypy 36 errors in 9 files, deliberately not a gate;
  coverage 90 % of `app/`, floor 88). Two claims already in the repo turned out to
  be wrong under measurement and are annotated in place rather than edited away.
- **CI**: a runtime-vs-dev isolation check that builds a throwaway venv and fails if
  the production graph contains test tooling, a coverage job, and a 3.11/3.12 matrix.
  3.10 is excluded because `rapidfuzz`'s own metadata forbids it.
- **Browser tests** (`tests_e2e/`, 12 scenarios against a real uvicorn and real
  chromium) covering the golden path, provenance honesty, the evidence lightbox, a
  correction surviving a reload, 390px scrolling and the unlock flow. Validated
  against a deliberate mutation rather than trusted.
- **HTTP hardening**: security headers, opt-in CORS, a sliding-window rate limit on
  the one endpoint that spends money, streamed upload caps, magic-byte type sniffing,
  an explicit pixel ceiling, threadpool offload for CPU-bound ingest, bounded
  in-memory report cache, optional run TTL, and crash logging that names the
  shipment.
- **Offline guarantee rebuilt**: a local proxy on loopback could forward real HTTPS
  requests from inside the guarded suite. The guard now permits only ports this
  process bound. `docs/adr/0007`.

### Phase 3 — measurement

- `scripts/evaluate.py` scores every captured extraction against ground truth the
  synthetic documents make knowable, and `docs/EVAL.md` is generated from it.
  Both providers: 75/75 exact on `compare_key`. One provider fabricated 6 values,
  which flips a clean shipment into conflicts. Recorded as a model limit; prompt
  tightening did not move it.
- Floors set at measured values so a regression fails the gate.

### Phase 2 — depth

- Field set grew from six to ten, driven entirely from `app/fields.py`; captures and
  fixtures rebuilt. Pinned: adding a field cannot break captures already on disk.
- `certificate_of_origin` as a fifth document type, with per-type expectations so a
  legitimately absent field is not reported as a gap.
- Cross-field sanity rules (`app/rules.py`) for shipments that are impossible rather
  than inconsistent, including net weight above gross.
- Explainable 0–100 risk score where every point traces to a named rule and the
  level is read off the score rather than decided beside it.
- Human corrections: overlay a value, re-compare instantly with no new API call, keep
  the model's original answer, revert cleanly, and mark any model prose written
  against the old values as superseded.
- Evidence navigation: jump a value to the page it came from, with the printed line
  quoted beside the image.
- PDF report export and an API-key-protected REST surface, with a browser session
  cookie because `<img>`, `EventSource` and downloads cannot set headers.

### Phase 1 — the honesty layer

- Raw model output, token usage and latency persisted per document; the transparency
  panel naming the model, the provider, the source and the cost of every answer.
- Model discovery against each host's own catalog, and the finding that NVIDIA
  publishes no modality field at all — so capability is inferred and labelled as
  inferred. An earlier version read a field that does not exist and the tests
  asserted invented payloads; both were wrong together.
- Advisory adjudication and a draft supplier email, both structurally unable to
  change a status.
- SSE progress with the 1-second poll kept as the fallback.
- Optional second-provider consensus that flags doubt and refuses to resolve it.
- Healthcheck, compose, CI workflow, and a deployment doc that states what is not
  true of a fresh instance.

### Phase 0 — the baseline

- The repository put under version control, starting from the hackathon prototype as
  imported at `59ecc52`.
- `docs/AUDIT.md`: every bug and risk found by a clean read, with priorities.
- The zero-network-call guarantee made mechanical, then proven false by a live
  request inside the guarded suite, then rebuilt.
- `make lint` and `make dev` made to actually work as advertised.

## Hackathon prototype — before the audit

Everything up to and including `59ecc52`. The core decision — a vision model
transcribes and deterministic Python compares — was already in place and was
confirmed by the audit as the thing not to rewrite.
