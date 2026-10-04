# Changelog

All notable changes, grouped by the phase that produced them. Every entry is a
commit in `git log`; the phase boundaries are the audit's prioritized plan, not
calendar weeks.

The format follows Keep a Changelog's grouping, but the entries are written as what
changed and what it cost, because that is the useful history for this repository.

## Unreleased — production-grade pass (2026-10-03 → 2026-10-04)

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
