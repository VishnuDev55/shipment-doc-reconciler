# Changelog

All notable changes, grouped by the phase that produced them. Every entry is a
commit in `git log`; the phase boundaries are the audit's prioritized plan, not
calendar weeks.

The format follows Keep a Changelog's grouping, but the entries are written as what
changed and what it cost, because that is the useful history for this repository.

## Unreleased — the deferred P2 set, and a flaky test that was a real bug (2026-10-29)

The first pass closed Critical and High and recorded the rest rather than dropping
them. This one works through that record. Each item was reproduced before it was
fixed, including the flake.

- **The offline guard authorized ports it had merely bound once (test
  infrastructure; see `PROGRESS.md`).** `tests/conftest.py` remembered every
  loopback port the process had *ever* bound, so a socket that was closed and gone
  still authorized a later connect to its port. That made one assertion in
  `test_offline_guard.py` depend on which ports the rest of the suite had used, and
  on this machine a connect to a dead loopback port hangs rather than refusing —
  which is why the symptom was a `TimeoutError` seen once in a full run.
  Authorization now follows the listener: the port is forgotten when the socket
  closes. Reproduced deterministically before the fix.
- **A header that is not ASCII produced a 500 before authentication (R21).**
  `hmac.compare_digest` raises `TypeError` on non-ASCII `str`; headers are decoded
  from raw bytes and a JSON body carries any Unicode, so `X-API-Key`,
  `Authorization: Bearer`, the session body and `X-Config-Token` each crashed the
  check instead of failing it. One `secret_matches()` now compares UTF-8 bytes
  through the same constant-time routine.
- **A document could start a formula in the exported CSV (R18).** `report.csv`
  inside the ZIP had no cell treatment, so a value beginning `=`, `+`, `-`, `@`, tab
  or CR ran when the reviewer opened the export. Prefixed with the conventional
  apostrophe; a leading `-` that parses as a number is left alone, because negative
  amounts are common and honest.
- **A filename could put control characters into a label (R17).** The label is
  rendered into the draft email, so a carriage return in a name started a new line
  of a letter. C0 controls and DEL are stripped; accents and CJK are kept, and the
  test pins that too.
- **The rate limiter believed the caller about who they were (R19).**
  `X-Forwarded-For` was trusted unconditionally, so a client could rotate its own
  bucket per request and the per-minute limit applied only to well-behaved clients.
  Now read only when `TRUSTED_PROXY` says a proxy you control writes it; otherwise
  the peer address is the identity, because it is the only part of a request the
  caller cannot choose.
- **The export that leaves the building had no policy of its own (R20).** The
  standalone report is saved, mailed and opened from a desktop, so it inherits no
  header. It carries a meta `Content-Security-Policy` (`default-src 'none'`) now —
  escaping is the control, this is the second lock for markup a later edit adds
  without escaping it.
- **Nothing had ever pruned a cache (R22).** `LIVE_CACHE_TTL_HOURS`, default 0,
  pruned at startup; the committed fixture tree is refused by name, because one
  script points its write directory there on purpose.
- **`/api/health` described the deployment to strangers (R23).** On a keyed
  instance, a caller without a key gets status, mode and `api_protected`; the model
  ids, hosts, version and retention window need the same key as everything else. The
  UI's two detail renderers tolerate the short payload and re-poll after a session
  opens.
- **Cost:** 27 tests added, 1159 → **1186 passed**, lint clean, `node --check` on
  `web/app.js`. The only item still deferred is pinning the transitive dependency
  set, which needs `pip-compile` against a live index this environment cannot
  reach.

## Unreleased — a whole-repository security review, and the six things it found (2026-10-29)

A read of the entire tree for security flaws, with four parallel review agents and
verification of every claim against the code before it was believed. One finding was
critical and reachable without a credential; the reproduction is in
`docs/AUDIT.md` under R10.

- **A shipment id was a path, and it served `.env` (R10, critical).**
  `/page/{shipment_id}/{path}` anchored its containment test to a directory the
  caller named, so `shipment_id=".."` moved the base up to the repository root and
  `.env` was inside it — no API key needed, because `render.yaml` set neither
  `API_KEYS` nor `CONFIG_TOKEN`. Fixed at the chokepoint: `RunStore.dir()`
  validates the id's shape, so all 11 callers inherit it, and `serve_page` requires
  the run to exist and re-checks containment against the resolved runs root. The
  test that appeared to cover this asked for a file no test ever created, so its 404
  proved nothing; the replacement writes the file first.
- **A live instance can no longer boot open (R11).** `DEMO_MODE=false` with an empty
  `API_KEYS` now raises at startup instead of coming up with every data-bearing route
  public and a healthy-looking `/api/health`. `ALLOW_OPEN_LIVE_DEPLOY=1` is the
  explicit opt-out. `render.yaml` gained `API_KEYS` and `CONFIG_TOKEN` as dashboard
  secrets, which also closes the `/api/config` mask finding, and `docker-compose.yml`
  passes `API_KEYS` through rather than defaulting it.
- **`/retry` was a billed call through an ungated door (R12).** It reached the vision
  model with no cap check and no limiter, so an instance that had correctly refused
  every upload for the day would still re-read documents one POST at a time. Both
  spend endpoints now share one gate, and a refusal happens before the document row
  is touched.
- **The daily cap is now a real ceiling (R13).** `cap_reached()` reads a ledger that
  gains a row only after a provider answers, so N simultaneous uploads all read the
  same empty day, all passed, and all spent. A reservation is now taken
  synchronously at dispatch and counts projected cost; it lives in memory only,
  expires at read time, and is released whatever the run's outcome — a persisted
  reservation held by a crashed process would have wedged the day for everyone,
  which is a worse failure than the race. `estimate_run_usd` reads `settings`, not
  module constants, and is deliberately pessimistic because an underestimate spends
  somebody's money past their cap while an overestimate only refuses a cheap upload.
- **A PDF's declared page size no longer decides our allocation (R14).** The 50 MP
  bomb ceiling guarded the Pillow path; `rasterize_pdf` handed every page to
  pypdfium2 at a fixed scale without reading the size it declared, so a sub-MiB file
  asking for 14400×14400 pt wanted ~3 GB per page. The scale is now clamped from
  `get_size()` before the render, and the loop has a wall-clock bound.
- **Live runs stopped writing into the tracked fixture tree (R15).** `CACHE_DIR`
  defaulted to git-tracked `fixtures/responses` and every live capture landed there,
  so `git add -A` could commit another company's shipment as a hand-authored sample.
  Reads and writes are now separate, with the committed tree checked first so a
  reviewed capture cannot be shadowed. `LIVE_CACHE_DIR` is new; `scripts/capture_summaries.py`
  names the committed tree explicitly, since producing committable captures is its
  job. CI fails if the suite leaves anything dirty under `fixtures/`.
- **A doc claim that was protecting nothing (R16).** `docs/SECURITY.md` cited
  `tests/test_lineitems.py` as pinning the standalone report's HTML escaping. No such
  assertion existed anywhere, in that file or in any other — and that artifact is the
  one which leaves the app as a bare file, inheriting no CSP header. It has a test
  now, proven non-vacuous by deleting one `esc()` call and watching three payloads
  fail.
- **Corrected, not just patched:** `docs/SECURITY.md`'s traversal and spend-throttling
  claims, `docs/DEPLOYMENT.md`'s blueprint sample (which showed `DEMO_MODE: "true"`
  against a `render.yaml` saying `"false"`), and the `README`/`SECURITY` lines that
  described `API_KEYS` as a recommendation.
- **Cost of the work:** 34 tests added, none removed or weakened; suite
  1124 → **1158 passed**, coverage 92% against an 88 floor; `pyflakes` and `ruff`
  clean. Each fix was proven red before it was green, and the traversal, the
  reservation race and the fixture-tree test were re-run red after the fact by
  reverting the specific change.
- **Deliberately out of scope** (Critical + High only, recorded in `docs/AUDIT.md`
  rather than dropped): `X-Forwarded-For` as rate-limit identity, CSV formula
  injection in the export, `hmac.compare_digest` raising a pre-auth 500 on a
  non-ASCII header, no cache or TTL pruning after startup, no CSP on the standalone
  report, and `/api/health`'s unauthenticated deployment fingerprint.

## Unreleased — live verification round (2026-10-19)

Paid live calls, budgeted at $8 and tracked in `scripts/spend.py` →
`runs/spend_log.json` (gitignored). What the round established, measured against
the real hosts:

- **Both providers answered live.** NVIDIA omni vision (strict/json_schema) and
  Nebius Nemotron-3-Nano summary; a new Nebius Nemotron VL
  (`nvidia/Nemotron-3.5-Super-VL`) appeared in `/v1/models` and was recorded as
  unpriced and unused.
- **The full pipeline runs live** — extraction, MiniCPM-V consensus, goods
  lines, summary, briefing, advisor — and all three demo verdicts reproduce
  (clean/conflicts/review). Primary accuracy 70/70 exact, p50 59 s / p95 67 s,
  ≈$0.01 per shipment.
- **The dominant live finding is the NVIDIA host, not the app.** Intermittent
  `503 ResourceExhausted` plus silent request hangs made documents fail or hang;
  the app degrades honestly (a failed key document weakens `conflicts`→`review`)
  rather than guessing. MiniCPM-V downgrades strict→json_object (already
  handled). A 90°-rotated phone photo returned nothing; German labels
  (`Warennummer`/`Bestellnummer`) were read as absent while decimal commas
  normalized fine. All recorded in `docs/EVAL.md` and the README live-findings
  list.
- **Consensus cost-benefit:** on every live divergence, NVIDIA was right and
  MiniCPM-V was wrong (its known PO/origin invention), and consensus caught them
  all.
- **The mypy baseline was corrected** from a stale-cache "39 errors in 10 files"
  to the fresh `--no-incremental` 8 errors in 3 files, after a type-annotation
  pass.
- Advisors and demo summaries were re-captured live into the cache; the advisor
  now replays captured notes in DEMO_MODE (cache-first, like the summary).

## Unreleased — the fallback, the harsher inputs, and a model promoted and
rejected (2026-10-21 → 2026-10-23)

Paid live calls again, budgeted and logged in `runs/spend_log.json` (gitignored).
This round settled four things by measurement rather than by argument:

- **A failed document is now re-read on the other host, and proved so live.** The
  cross-provider vision fallback (`VISION_FALLBACK`) routes one document, stamps
  the routing note in the report, and never fires in DEMO_MODE.
  `scripts/live_fallback_check.py` points the primary at a dead port and asserts
  the document still arrives on `nebius / openbmb/MiniCPM-V-4_5`.
- **Non-English and harsh inputs, measured.** The German and French stress pages
  (both generated by `scripts/build_stress_inputs.py`, so still synthetic) read
  10/10 on MiniCPM-V, and the phone photo went from 6/10 with an invented value to
  8/10 with none once orientation preprocessing was on. **None of those three has an
  omni number**: it hung past 150 s on the German page and on both photo variants,
  and the French case was only ever run on the Nebius host. The docs say "not
  measured" rather than implying a comparison. Fixing the German case exposed a real
  normalizer bug: `PACKAGE_KINDS` knew only English packaging words, so `100 KARTONS`
  and `100 cartons` were a unit-basis conflict between two documents describing one
  pallet.
- **`nvidia/Nemotron-3.5-Super-VL` was tested as a candidate default against a rule
  written before the run** (3/3 verdicts, 0 invented, exact ≥ 99%): four live passes
  through the real pipeline — three demo shipments plus the 19-group planted bundle,
  consensus off and on, each twice — gave **3/3 verdicts and 0 invented in every
  pass**, but **74/75 (98.7%)** fields exact against omni's 75/75, with the same
  single miss reproduced four times out of four. It did not clear its own bar, so
  **omni stays the default**, Super-VL stays selectable, and no "Nebius-only" preset
  was added. `fixtures/model_comparison.json` carries the numbers; `docs/EVAL.md` is
  generated from it.
- **A real concurrency bug in the run store.** Tests that wrote the same report from
  eight threads failed: the atomic write used one shared `report.json.tmp`, so two
  writers could collide and the loser raised `FileNotFoundError`, and Windows also
  refuses a rename onto a file a reader holds open. Each write now takes its own
  temp file via `mkstemp`, and the rename retries on `PermissionError`.
- **Spend hygiene:** `scripts/live_compare.py` records tokens for every live call,
  including the ones that failed to parse (previously invisible), and `--help` no
  longer spends a call before printing usage.
- **CI ran for real, and the first run was red.** Pushed to GitHub on 2026-10-23,
  all four Python jobs failed dependency resolution while `docker` passed:
  `requirements-dev.txt` pinned `pytest==9.1.1` while `pytest-playwright==0.7.1`
  declares `pytest<9`, so the dev extras were uninstallable on a fresh clone — a
  bug no local run could see, because this machine has 8.4.2 installed (as
  `requirements.lock.txt` always said). Pinning pytest to 8.4.2 fixed it; the next
  run was green across lint-and-test on 3.11 and 3.12, the coverage floor, the
  chromium DEMO_MODE browser suite, and the docker build with its smoke test and
  `HEALTHCHECK`. The red run is cited in `docs/DEPLOYMENT.md` rather than dropped,
  and the README badge points at the live workflow so the status cannot be
  hand-tuned.

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

### Deployment readiness

- **The container never bound the port Render assigns.** The Dockerfile started
  uvicorn on a hardcoded 8000, so on Render — which injects `$PORT` and routes
  to it — the service would have answered its own health check but been
  unreachable from the outside. `CMD` now runs in shell form and binds
  `${PORT:-8000}`, and the in-image `HEALTHCHECK` probes the same port.
- `/api/health` now reports `mode` (demo/live), `version`, and `git_commit`
  alongside the provider status, which is expressed as `configured` booleans.
  The commit is read from the environment first (Render's `RENDER_GIT_COMMIT`,
  Actions' `GITHUB_SHA`, or `GIT_COMMIT`, which the Dockerfile bakes in because
  the image does not copy `.git`), then from a local checkout's `.git`, and is
  cached so a load-balancer poll never shells out. The key itself is still never
  echoed, and a test now asserts that.
- A cold start in `DEMO_MODE` announces itself: the first element in `<main>` is
  a banner reading "Demo mode: replaying captured real model output", so a first
  visitor cannot mistake a replayed response for a live call or a fabrication.
- `docs/DEPLOYMENT.md` gained exact Render steps, the env-var surface, and a
  post-deploy checklist (cold start → `/api/health` → the contradiction demo →
  download the ZIP).

### Nebius transparency, consensus and the advisor

- **One step table, three views.** `app/transparency.py` computes the model steps
  of a run once — per-document extraction, the second-reader consensus pass, the
  summary, the draft email and each advisory second opinion, with provider,
  model, provenance, attempts, tokens and latency — and the web panel, the
  standalone `report.html` and the PDF all render it from that one source, so the
  three can no longer disagree about which model did what. The "Powered by Nebius
  Token Factory + NVIDIA Nemotron" badge was made honest: it reports the call
  count, says "no live call" on a replay, and names hand-authored values as
  hand-authored rather than "replayed".
- **Consensus is a drawer toggle now**, not just an env var. `CONSENSUS`/the
  **Second reader** checkbox round-trips through `/api/config` and flips the
  orchestrator's second-reader pass; the one-line explanation says it never
  changes a verdict and costs an extra vision call per document.
- **The advisor now also reads withdrawn evidence.** For every value the
  evidence check excluded, a Nebius model writes one short note guessing at the
  cause (page-break reflow, OCR ligature). Same contract as the existing
  adjudicator: deterministic evidence in, structured validated JSON out,
  length-capped, cached by the evidence's own signature, silent on any failure —
  and it can never reinstate the value. Pinned with a fake transport in both
  directions: withdrawal and verdict are byte-identical advisor-on vs
  advisor-off, and a failing advisor never fails the run.

### Live progress: per-stage events and a per-document timeline

- The SSE stream now carries two kinds of frame. `event: stage` publishes each
  document's transition — `queued`, `reading`, `verifying`, `comparing`, `done`,
  `failed` — with the provider/model and elapsed milliseconds; `event: report`
  still carries the decisions. Stage events ride the same bounded fan-out as
  report frames, so they inherit drop-oldest backpressure and the existing
  polling fallback for free: a client with no `EventSource` still ends at the
  same terminal report, just without the in-flight stage detail.
- The UI renders those frames as a per-document timeline under the progress
  line: document, current stage, the provider and model working on it, and
  elapsed seconds. It is advisory — it yields to the settled documents table
  the moment the run is done, and it is never the source of a verdict.
- Pinned offline with the existing HTTP harness: a stage dict fans out, a real
  (cached) run emits reading→done between its report frames, and the route
  serializes `event: stage` correctly. One DEMO_MODE browser test drives the
  timeline handler directly, since a cached run finishes faster than a browser
  can watch it.


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
