# Engineering audit — shipment-doc-reconciler

Audited 2026-10-02, from a clean read of `app/`, `web/`, `tests/`, `scripts/`,
`fixtures/` and every build/config file. Line references are to the state at the
baseline commit `chore: import the hackathon prototype as the audit baseline`.

**Verdict:** the hard part is genuinely good and should not be rewritten. The core
decision — a vision model only transcribes, and deterministic Python does all
comparison — is implemented with unusual discipline, and it holds. What is missing
is everything around the algorithm: the offline guarantee had no enforcement
behind it, the documentation asserted proofs the repo could not produce, there was
no version control, and there is no auth around an endpoint that rewrites the
server's API keys.

Priority below is P0 (should be fixed before anyone relies on this) / P1
(production-grade but not blocking) / P2 (quality-of-life).

---

## What works, and is worth keeping as-is

- **The model never does arithmetic.** Extraction produces `value/unit/raw` and
  nothing else; every judgement happens in `app/normalize/` and
  `app/compare/__init__.py`, both pure and synchronous. This is the correct
  architecture for a reconciliation tool, and it is enforced by the module
  boundaries rather than by trust.
- **Tolerances are declared, not improvised.** Frozen constants in one block at
  `app/compare/__init__.py:25-36` — weight 0.1%/1 kg/2%, money 0.01%/1%, party
  100/90/70, `LOW_CONFIDENCE = 0.5`. A reviewer can dispute a number in one line.
- **Provenance is recorded, not assumed.** `report.json` carries
  `prompt_version`, `normalizer_version`, `comparator_version` and `model`
  (`app/config.py`), so a cached response cannot be silently reconciled against a
  newer ruleset. Replaying an authored fixture is never reported as a model
  result.
- **ISO 6346 check-digit arbitration** on container numbers rather than string
  equality (`app/compare/__init__.py:260-307`). A typo'd container is caught by
  the standard's own arithmetic, which is a much stronger signal than fuzzy
  matching. Verified against the published worked example (`CSQU305438` → 6185 →
  check digit 3).
- **A real capability probe, not a hardcoded assumption.** `ChatClient.probe`
  (`app/client.py:250-310`) determines per endpoint whether strict
  `json_schema` is honoured, and degrades to `json_object` then plain, with a
  separate 3-try budget for 429/5xx so a saturated worker is not mistaken for a
  missing capability.
- **The extraction ladder** — `initial` → `corrected` → `simplified`
  (`app/client.py:404-453`) — gets one bad field without failing the document.
- **Path traversal is handled properly.** `/page/{id}/{path}` resolves and then
  checks `Path.is_relative_to(base)` plus `is_file()` (`app/main.py:340-346`);
  upload names are allowlisted and directory-stripped (`app/store.py:79-83`,
  `:120-122`). `build_zip` only writes (`app/export.py:301-332`) — there is no
  extraction path anywhere, so zip-slip is not reachable.
- **Key masking is real.** `GET /api/config` returns `key[:5] + "…" + key[-4:]`
  (`app/main.py:114-124`) and `POST` drops an echoed mask instead of storing it
  (`app/main.py:174`).
- **`make dist` uses an allowlist, not a denylist** (`Makefile:58-79`), so `.env`,
  `runs/` and stray artifacts cannot leak into the distributed zip by forgetting
  to add a pattern.
- **Single-stage Docker with no `apt-get`.** The dependency set was evidently
  chosen so everything resolves to manylinux wheels; that is a real constraint
  that was respected rather than worked around.

---

## Bugs

### B1 — `runs/{id}/raw/` is always empty; the transparency data is already being thrown away · P0
`ChatClient` accepts `raw_dir` (`app/client.py:143`) and `persist_raw` writes to
it (`app/client.py:312-319`), but `Reconciler` constructs the client without it:

```
app/orchestrator.py:268   self.client = ExtractionClient(self.settings)
app/orchestrator.py:309   (same omission in reconfigure)
```

`app/store.py:43` still creates the directory. So every run has an empty `raw/`
and the ZIP ships no raw model output (`app/export.py:325-331`).

Related: `AttemptLog` already carries per-attempt `usage` and latency
(`app/client.py:87-92`) and `transport_call` returns a usage dict
(`app/client.py:223-234`), but `apply_outcome` persists only an attempt count and
total latency (`app/orchestrator.py:207-208`), and the summary's usage is
explicitly discarded — `text, _usage = ...` (`app/orchestrator.py:500`).

This matters beyond tidiness: tokens and latency are the only evidence that a
result was produced by the model claimed, on the host claimed. Today that
evidence is generated, held in memory for microseconds, and dropped.

### B2 — `make lint` cannot run on a fresh clone · P0
`Makefile:31` invokes `pyflakes`; `pyflakes` appears nowhere in
`requirements.txt`. Verified: a fresh project venv built from
`requirements.txt` reports `No module named pyflakes`. The README advertises the
command, so the first thing a reviewer types after cloning fails.
(Fixed in this audit by declaring the dependency; lint itself reports zero findings.)

### B3 — `make dev` is described as live but is hardcoded to demo mode · P1
`README.md:207` told the reader `make dev` is "live; paste keys into Configure",
while `Makefile:27` is `DEMO_MODE=true $(PY) -m uvicorn ...`. The help text at
`Makefile:8` says DEMO_MODE. The README was wrong; corrected. The label was
fixed rather than the Makefile, because a live `make dev` with no key is a
`ConfigError`, which is a worse first-run experience than a working demo.

### B4 — a background failure can vanish silently · P1
`asyncio.create_task(self._process(...))` at `app/orchestrator.py:416` stores the
task in `self._tasks` but attaches no done-callback and no exception logging. If
`refresh_report` or `store.write` raises after the first document settles, the
run sticks at `running` and the reason exists only in an unretrieved task
exception. The user sees a spinner forever.

### B5 — the offline guarantee had no mechanism · P0, now fixed
`README.md:519-522` and `:539-542`, `PROGRESS.md:50` and `:317` all described a
socket guard that raises when anything connects to the provider hosts. No such
code existed: there was no `conftest.py`, and `tests/__init__.py` was empty. The
suite was offline only because each test happens to pass `demo_mode=True`
(`tests/test_api.py:25-28`) or inject a fake into the private slot
(`tests/test_client.py:109-113`).

Enforced now by `tests/conftest.py`, with `tests/test_offline_guard.py` keeping
the wall itself under test. The documented host list was also the wrong shape —
naming five hosts means the sixth is allowed.

---

## Risks

### R1 — unauthenticated write access to the server's API keys · P0
`POST /api/config` (`app/main.py:157`) has no auth, and `Reconciler.reconfigure`
mutates the process-global settings object in place
(`app/orchestrator.py:277-311`). Anyone who can reach the port can replace the
NVIDIA and Nebius keys for every other user of that instance, or flip
`demo_mode`.

The design decision is defensible — it is a local tool, and `render.yaml:60-61`
sets `GUI_CONFIG_ENABLED="false"` for the public instance — but the **default is
open** (`app/config.py:65`). Masked key material (`nvapi…1234`) is additionally
readable by any unauthenticated caller via `GET /api/config` whenever the flag is
on. Needs a token, or the default flipped to off with an explicit local opt-in.

`app/store.py:22` states the current posture plainly: *"Unguessable ID is the
whole access-control story for a public demo."* That is honest, and it is not
enough for anything long-lived.

### R2 — uploads are fully buffered before the size check · P1
```
app/main.py:246   data = await upload.read()
app/main.py:247   if len(data) > settings.max_upload_bytes:
```
The 25 MB limit (`app/config.py:72`) is enforced after the entire body is in
memory, and up to 5 files are accepted per request. A larger limit is a DoS
multiplier, not a protection. Needs a streaming read with an early abort.

### R3 — blocking work runs on the event loop · P1
PIL/pypdfium2 rasterization is called directly from the async request path
(`app/orchestrator.py:340`, via `app/ingest.py:108`), and every
`save_upload`/`save_pages`/`store.write` is synchronous file IO
(`app/orchestrator.py:441-442`). `load_fixture` additionally does sync
`read_bytes` and can hold a request up to 600 s (`app/main.py:320-324`). Under
concurrency these stall unrelated requests. `run_in_threadpool` is the fix.

### R4 — concurrent documents race on the shared probe · P2
`probe()` mutates `self.json_mode` on the single shared client
(`app/client.py:264-266`) and is awaited from each fan-out
(`app/client.py:397`). Two documents starting cold can both probe — duplicated
spend — and the mode can change mid-flight for the other. A lock, or probe
before fan-out.

### R5 — unbounded process growth · P2
`self._reports` (`app/orchestrator.py:270`) is only ever added to
(`:411`), never evicted; `runs/` has no TTL or compaction. A long-lived instance
accumulates every report in memory forever.

### R6 — filesystem as the database · P2
`list_shipments` walks directories and parses every `report.json` on each call
(`app/store.py:96-117`), and `_find_authored` globs the whole cache on every
miss (`app/cache.py:92`). Fine for a demo; it is the scaling ceiling, and no
index or lock guards concurrent writes to one run.

### R7 — no HTTP hardening at all · P1
Zero `add_middleware`, `CORSMiddleware`, security headers or rate limiting
anywhere in `app/`. `pillow` decompression-bomb limits are not set. PDF pages are
capped at 6 (`app/config.py:71`) but image dimensions are not capped before
resize. No `HEALTHCHECK` instruction in the Dockerfile (Render compensates via
`healthCheckPath`, `render.yaml:46`).

### R8 — supply chain and reproducibility · P1
`requirements.txt` is entirely ranged with no lockfile; `openai>=1.40,<4` spans
two major versions and `python-multipart>=0.0.9` is unbounded above. Test-only
deps (`pytest`, `httpx`) sit in the runtime file, so the production image installs
the test suite's dependencies. Two different clones can resolve differently and
only one is tested.

### R9 — the "verified live" claims are dated and currently uncheckable · P1
`providers.py:27`, `:32` and `:36-38` record model choices as "Verified live
2026-09-23", and README's verification table rests on captures committed under
`fixtures/live_responses/raw/`. Those artifacts are the right instinct. But no
key was available during this audit, so nothing on that table was re-run: model
availability on both hosts changes without notice, and
`nvidia/nemotron-nano-12b-v2-vl` is on that table precisely because it returned
**410 Gone** mid-project. `README.md` now says this explicitly.

---

## Tech debt

- **Mutable global singletons.** `@lru_cache get_settings()` (`app/config.py:111`)
  plus `_reconciler` (`app/main.py:35-48`) mean the config is process-wide and
  mutated at runtime. 18 tests in `tests/test_config.py` depend on the current
  shape, so this needs a seam, not a rename.
- **Model names hardcoded in two places** (`app/providers.py:30`, `:34`, `:39`)
  and duplicated as an HTML fallback string (`app/export.py:289`). The export
  layer can outlive a model swap and print a stale name.
- **`import re` inside a function body** (`app/client.py:539`).
- **`probe_log` writes to stderr with `print`** rather than the logging module
  (`app/client.py:281-308`), so probe outcomes cannot be correlated with a run.
- **No structured logging, no request/shipment ID in log lines, no error
  taxonomy.** There is nothing to grep when a production run misbehaves.
- **No `docs/`, no CI, no CHANGELOG, no ADRs** — decisions like "two providers",
  "no OCR fallback", "filesystem database", "deterministic comparison" and the
  ISO 6346 arbitration exist only as code comments and as prose in `SUBMISSION.md`,
  which is a hackathon artifact, not an engineering reference.
- **Not a git repository.** The delivered folder had no `.git`, although
  `PROGRESS.md:43` recorded it as "git init-ed and staged (119 files)".
  Initialized at this audit; history now starts from the baseline commit.
- **Coverage holes.** `app/store.py` and `app/narrative_cache.py` have no direct
  tests — they are only reached through `Reconciler`. `app/export.py` is covered
  for standalone-HTML banners (`tests/test_pipeline.py:396-430`) and the ZIP
  endpoint, `app/summary.py` for prompt building and plausibility, and
  `app/prompt.py` only for `example_output` (`tests/test_providers.py:379-392`).

---

## Prioritized plan

| # | Item | Pri | Notes |
|---|---|---|---|
| 1 | Socket guard + self-test | P0 | **done** — `tests/conftest.py`, `tests/test_offline_guard.py` |
| 2 | Correct every stale test count and false guard claim | P0 | **done** — README, SUBMISSION, PROGRESS |
| 3 | Declare `pyflakes`, or drop `make lint` | P0 | **done** — declared |
| 4 | Baseline commit + conventional history | P0 | **done** — local only; no remote authorized |
| 5 | `docs/AUDIT.md` | P0 | **this file** |
| 6 | Keep `raw_dir` + per-attempt usage/latency; drop `_usage` discard | P0 | **done** — `tests/test_transparency.py`, panel in `web/` |
| 7 | Auth or default-off for `POST /api/config` | P0 | R1 |
| 8 | Done-callback and exception logging on the background task | P1 | B4 |
| 9 | Model discovery instead of hardcoded literals | P1 | **done** — `app/discovery.py`; logic verified against replayed catalogs, **unverified live** (no key). Export's duplicate literal removed |
| 10 | Nebius adjudicator for the 70-89 band and low-confidence cases | P1 | **done** — `app/adjudicate.py`; opinion cannot change a verdict, asserted by `tests/test_adjudicate.py`. Deterministic customs-risk strip and a guarded draft email added alongside. **Unverified live** (no key) |
| 11 | Streaming upload limit + magic bytes + PIL bomb limit | P1 | R2, R7 |
| 12 | Off-loop ingest and file IO | P1 | R3 |
| 13 | Lockfile, runtime/dev split, CI matrix | P1 | R8 |
| 14 | Eval harness and `docs/EVAL.md` | P1 | Phase 3 |
| 15 | Probe race, `_reports` eviction, `runs/` TTL | P2 | R4, R5 |
| 16 | Structured logging + IDs; docs/ set; ADRs | P2 | Phase 5 |

---

## Verification log

| Check | Command | Result |
|---|---|---|
| Suite before changes | `pytest tests/ -q` | **286 passed in 11.70s** |
| Suite after guard | `pytest tests/ -q` | **297 passed in 9.34s** — 286 existing untouched, 11 added |
| Guard fires | `test_offline_guard.py` | remote connect, `connect_ex`, `create_connection`, `getaddrinfo`, `gethostbyname` all raise; loopback still works |
| Live client contained | `test_a_live_mode_client_cannot_escape_to_the_network` | live-mode `ChatClient` with a key passing the placeholder check dies on the socket wall |
| Lint | `pyflakes app scripts tests` | 0 findings |
| Secrets | `git grep -I -i -E` for `nvapi-*`, `sk-*`, `AKIA*`, private-key blocks | 0 matches in the tracked tree; no `.env` exists |
| Container | `docker build -t shipment-doc-reconciler .` | exit 0, `f61257df4238`, 475 MB, no `apt-get` |
| Container | `docker run -d -p 8000:8000` + `GET /api/health` | `{"status":"ok","demo_mode":false,…}` — live by default, both `key_present: false` |
| Container | `GET /api/fixture/shipment_conflict` | 200, returns `shipment_id` and `state: "done"` |
| Container | `GET /api/config` | 200, `editable: true`, no key material in the body |
