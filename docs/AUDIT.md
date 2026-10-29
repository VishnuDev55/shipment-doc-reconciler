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
The README's quick start told the reader `make dev` is "live; paste keys into
Configure" (line numbers are not quoted here any more: the README was cut to a
front door on 2026-10-23, and a citation that points at a moved line is worse than
no citation),
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
The README's Tests section, `PROGRESS.md:50` and `:317` all described a
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

**Addressed 2026-10-02:** `CONFIG_TOKEN` gates both config endpoints on a
constant-time compared `X-Config-Token` header, and the drawer prompts for it
without persisting it. Left blank by default, so local single-user use is
unchanged — forcing a secret on a laptop buys nothing. A public deployment should
still prefer `GUI_CONFIG_ENABLED=false`.

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

### R7 — no HTTP hardening at all · P1, closed 2026-10-04
Zero `add_middleware`, `CORSMiddleware`, security headers or rate limiting
anywhere in `app/`. `pillow` decompression-bomb limits are not set. PDF pages are
capped at 6 (`app/config.py:71`) but image dimensions are not capped before
resize. No `HEALTHCHECK` instruction in the Dockerfile (Render compensates via
`healthCheckPath`, `render.yaml:46`).

*Closed.* Security headers and an opt-in CORS block are wired, uploads are read
under a byte cap, an explicit pixel ceiling plus magic-byte sniffing replaced the
suffix trust, and `HEALTHCHECK` has been in the Dockerfile since Phase 1 — so the
last sentence above is no longer true even as a historical one.

### R8 — supply chain and reproducibility · P1, closed 2026-10-04
`requirements.txt` is entirely ranged with no lockfile; `openai>=1.40,<4` spans
two major versions and `python-multipart>=0.0.9` is unbounded above. Test-only
deps (`pytest`, `httpx`) sit in the runtime file, so the production image installs
the test suite's dependencies. Two different clones can resolve differently and
only one is tested.

*Closed.* The file is pinned, split runtime/dev, and mirrored by
`requirements.lock.txt` and `pyproject.toml`. **One part of this finding is now
wrong in a way worth knowing before repeating it:** `httpx` is not a test-only
dependency. `app/discovery.py` constructs `httpx.AsyncClient` to read provider
model catalogs, so moving it into the dev file would break model discovery inside
the container. It *was* test-only when this was written — discovery came later —
so the finding is not sloppy, it is simply predating the code. Check an import
graph, not a memory, before classifying a dependency.

### R9 — the "verified live" claims are dated and currently uncheckable · P1
`providers.py:27`, `:32` and `:36-38` record model choices as "Verified live
2026-09-23", and README's verification table rests on captures committed under
`fixtures/live_responses/raw/`. Those artifacts are the right instinct. But no
key was available during this audit, so nothing on that table was re-run: model
availability on both hosts changes without notice, and
`nvidia/nemotron-nano-12b-v2-vl` is on that table precisely because it returned
**410 Gone** mid-project. `README.md` now says this explicitly.

---

## Risks found by the 2026-10-29 whole-repository review

Six findings, one of them critical. Each is written in the shape the earlier ones
use: what the attack is, where it lives, and what closed it. A cloud scan of the
same tree was submitted the same day and its findings are not folded in here yet.

### R10 — the shipment id was a path, and it served `.env` · P0, closed 2026-10-29
`GET /page/{shipment_id}/{path}` (`app/main.py:807`) anchored its own containment
test to a directory the caller named:

```
base = rec.store.dir(shipment_id).resolve()      # dir() was `self.root / shipment_id`
target = (base / path).resolve()
if not target.is_relative_to(base) ...
```

`shipment_id=".."` made `base` the **repository root**, so `.env` was inside it.
Reproduced against the real path math before any fix:

```
shipment_id='..'  path='.env'  ->  base = <repo root>
                                    target.is_relative_to(base) = True   exists = True
```

It was reachable without a credential, because `require_api_key` returns
immediately when `API_KEYS` is empty (`app/auth.py:69-70`) and `render.yaml` set
neither `API_KEYS` nor `CONFIG_TOKEN` while running `DEMO_MODE: "false"`. The test
that appeared to cover this (`tests/test_api.py:195-200`) only traversed through
`path`, always with a valid id, and asked for a `secret.txt` that no test ever
created — so its 404 proved nothing.

Not a one-route bug: `RunStore.dir()` is where a caller-supplied id meets the
filesystem, and 11 callers go through it.

**Addressed:** the shape is validated at the chokepoint (`_ID_RE`,
`app/store.py:42`, enforced in `dir()` at `:111`), so every route, export and
script inherits it rather than each remembering to. `self.root` is resolved once
in `__init__` and `root_resolved` is what containment is measured against — before,
a relative `runs` made the comparison depend on the cwd. `serve_page` now requires
`store.exists()` before it resolves anything and asserts containment against the
runs root in addition to the run directory, so the answer no longer depends on a
name the caller chose. `store.py` joins nothing else: `exists()` and `read()`
return "no such run" for a malformed id instead of raising, because a junk URL
must not surface as a 500 with a traceback. `export._evidence_data_uri` got the
same guard as defence-in-depth (it is not currently reachable — `image_path` is
server-generated). Tests: the 11 parametrized id shapes in `tests/test_store.py`,
`test_page_route_blocks_traversal_through_the_id_segment` (which writes the secret
first, so the 404 means something), and
`test_no_route_answers_a_malformed_id_with_a_500` across six routes.

### R11 — a live instance could boot with no key to check anybody against · P0, closed 2026-10-29
The same blank `API_KEYS` that makes R10 reachable made every other data-bearing
route public, and nothing said so: the app booted, `/api/health` returned 200, and
the deployment looked healthy while serving other people's commercial documents to
anyone who found the URL. `docs/SECURITY.md` already called a set `API_KEYS` the
minimum for a live instance. The rule was documentation, and documentation is not
a control.

**Addressed:** a FastAPI `lifespan` (`app/main.py:90`) raises `RuntimeError` when
`demo_mode` is false and `api_key_list` is empty, naming `API_KEYS` as the remedy
and pointing at the doc that says so. It reads the **injected** reconciler's
settings, so a test that configures its own instance is judged by what it actually
set. `ALLOW_OPEN_LIVE_DEPLOY=1` lifts it for an instance genuinely behind
something that authenticates — named for what it does, so nobody enables it by
reading a paragraph about disabling security. `render.yaml` now carries `API_KEYS`
and `CONFIG_TOKEN` as `sync: false` dashboard secrets, which means an unset secret
produces a boot that fails loudly instead of a deployment that is quietly open.
Verified by booting all three ways: live with no keys refuses, live with keys
serves and answers 401 to an unauthenticated `/api/config`, and the escape hatch
starts. Tests in `tests/test_public_instance.py`.

### R12 — `/retry` reached a billed call through a door nobody gated · P1, closed 2026-10-29
`POST /api/shipments/{id}/retry` (`app/main.py:683`) had no cap check and no
limiter, yet `Reconciler.retry_document` goes to the same vision model an upload
does. The consequence was specific: an instance that had spent its day's cap and
correctly refused every upload would still happily re-read documents one at a
time, indefinitely, for the price of a POST.

**Addressed:** the two endpoints that can bill now share `_gate_spending()`
(`app/main.py:448`) — the ledger check, then the per-client limiter, then the
dispatch-time reservation below. The refusal happens before the document row is
touched, so a rejected retry cannot leave a row sitting at `status="running"` with
nothing running behind it. Test: `test_a_retry_is_refused_when_the_day_has_no_money_left`,
which asserts the row's status is what it was before the request.

### R13 — the daily cap was enforced against a ledger that lags the truth · P1, closed 2026-10-29
`cap_reached()` reads `spend_log.json`, and that file gains a row only once a
provider has answered. The check at the door, the dispatch, and the write were
three different moments:

```
read(ledger) -> check cap -> await asyncio.create_task(...) -> provider answers -> write(ledger)
```

N uploads arriving together all read the same empty ledger, all pass, and all
spend. A `$2.00` ceiling was a suggestion at N=40, and the caller had to do nothing
cleverer than press enter on a form.

**Addressed:** a reservation is taken at the point of dispatch, synchronously, and
counts **projected** spend — what the ledger holds plus what this process has
decided is likely to be spent but has not yet seen (`app/budget.py:275`, called
from `orchestrator.py:646` before the first `await`). Three properties make it safe
to add:

- `reserve()` never awaits, so no second upload can interleave between reading the
  projection and writing the reservation. The `threading.Lock` serves the threaded
  `record_call` path; it is not what closes the race.
- State is **memory-only, never on disk.** A persisted reservation that its holder
  crashed behind would wedge the day for everyone — a worse failure than the race.
- A leaked reservation expires at read time (TTL, `SPEND_RESERVATION_TTL_SECONDS`,
  default 3600), mirroring how `RateLimiter.check` prunes, so there is no sweeper
  task that can fail to run. Released in `_task_done` for all three outcomes, and
  immediately when a shipment turns out to have no dispatchable documents.

The estimate (`estimate_run_usd`) reads `settings`, not module constants, because
`config.py` ships 5 documents / 6 pages while `render.yaml` narrows them to 3 / 4 —
pricing the wrong pair misprices every run. It is deliberately pessimistic (full
backoff ladder, both reads when consensus is on, the extra read when line items
are on) because the two directions of error are not equal: an underestimate spends
somebody's money past their cap, an overestimate refuses an upload that would have
been cheap. `test_one_document_always_fits_an_empty_day` guards the failure mode
that choice creates — an estimate so generous that nobody can ever upload again,
which would be worse than the bug. `test_n_uploads_cannot_all_pass_an_empty_ledger`
drives six concurrent uploads against a day that fits two, with the ledger empty
throughout, so any refusal can only have come from the reservation.

### R14 — a PDF's declared page size decided how much memory we allocated · P1, closed 2026-10-29
`MAX_IMAGE_PIXELS` (50 MP) is the ceiling that refuses a decompression bomb, and it
was enforced on the Pillow path only. `rasterize_pdf` (`app/ingest.py`) handed every
page to pypdfium2 at a fixed `scale=2.0` without looking at the size that page
declared, and the renderer allocates `width*scale x height*scale` from it. A
sub-MiB file declaring 14400x14400 pt pages asked for ~830 MP — about 3 GB of
bitmap per page — through a code path the guard did not cover.

**Addressed:** `_render_scale()` (`app/ingest.py:258`) is computed from
`page.get_size()` **before** `render()`, clamped both to the pixel ceiling and to
twice the long edge the page is about to be resized to anyway. The page is drawn
smaller rather than refused, so a legitimate large-format document still comes
back readable. The render loop also has a wall-clock bound per document
(`PDF_RENDER_DEADLINE_SECONDS`), checked between pages, because one document
holding the CPU is the same denial of service whether it does it on page 1 or
page 6. Test asserts on the scale actually passed to `render` — the page that
comes back is resized either way, so an assertion about the *output* would have
passed with or without the fix, which is exactly how the first draft of that test
was wrong.

### R15 — live runs wrote into the tracked fixture tree · P1, closed 2026-10-29
`CACHE_DIR` defaulted to `fixtures/responses`, a directory git tracks **on purpose**:
its 51 files are reviewed captures that `docs/ARCHITECTURE.md` counts and DEMO_MODE
replays byte-for-byte. Every live extraction wrote its answer there. So one
`git add -A` could commit another company's shipment as if it were a hand-authored
sample, a machine-generated file could sit in front of a reviewed one under the same
key, and the report's "captured response" provenance line stopped being a claim
anybody could check.

**Addressed:** the cache is now read-dirs and one write-dir (`app/cache.py:47`,
`NarrativeCache`, `AdjudicationCache`), with reads checking the committed tree
**first** so a reviewed capture cannot be shadowed. The write target is decided at
use time by `cache_write_dir()` (`app/config.py:330`) rather than stored on the
settings object, because tests build settings with `model_copy(update=...)` and a
validator does not run on that path — a resolved-at-boot answer would have been
wrong in the direction that writes into the tracked tree. `LIVE_CACHE_DIR` is not
under `RUNS_DIR`: `store.purge_older_than()` rmtree's every subdirectory of the runs
root older than `RUN_TTL_HOURS`, which render.yaml sets to 24.
`scripts/capture_summaries.py` names the committed tree explicitly, since its whole
job is to produce committable captures.

The headline test snapshots `fixtures/**` by `(path, sha256)`, drives a live run
whose faked transport records usage so both the cache write and the spend row fire,
and requires the tree byte-identical afterwards. Digests rather than a read-only
tree because Windows ACLs are unreliable and `_store_success` swallows `OSError`, so
a blocked write would have looked like success. CI now fails the build if the suite
leaves anything dirty under `fixtures/`. Two scripts the plan expected to need
overrides — `probe_live.py` and `evaluate_planted.py` — turned out not to write to
the tracked tree at all, and were left alone.

### R16 — the standalone report's escaping was cited by the docs and tested by nothing · P2, closed 2026-10-29
`docs/SECURITY.md` pinned "untrusted text is data, never instructions" with a
reference to `tests/test_lineitems.py`. No escaping assertion existed in that file,
or anywhere else for `standalone_html` — and that artifact is the one that leaves
this app as a bare file (mailed, saved to a desktop, opened from a stick) and so
inherits none of the `Content-Security-Policy` the served pages get.

**Addressed:** `test_document_text_cannot_mark_up_the_standalone_report` covers four
payloads through the reason, document label, raw value and canonical value, checking
the **quote-escaping** form because these values land inside attributes as well as
between tags. Verified non-vacuous: deleting the single `esc()` around the raw-value
cell fails three of the four. `docs/SECURITY.md` now cites the tests that exist.

### R17 — a filename could put control characters into a label · P2, closed 2026-10-29
`short_label` (`app/orchestrator.py`) took `Path(filename).name` and used it
directly. The label is not only displayed: it is rendered into the draft email and
into the report, so a carriage return in a name starts a new line of a letter and
can forge a header-shaped line in a text client, and a tab or an escape sequence
reorders what a terminal shows about a shipment.

**Addressed:** C0 controls and DEL are stripped at the join
(`_CONTROL_CHARS` in `app/orchestrator.py`). Accents and CJK are kept -- the test
pins that too, because the risk in this fix is over-cleaning a reviewer's own
data.

### R18 — a document value could start a formula in the exported CSV · P2, closed 2026-10-29
`report.csv` inside the ZIP was written from document text with no cell treatment.
A value beginning `=`, `+`, `-`, `@`, tab or CR opens in Excel, LibreOffice or
Sheets as an expression, so a shipment document saying
`=HYPERLINK("http://…","click")` runs when the reviewer opens the export. No click,
no macro prompt -- and the person opening it is the one holding the account.

**Addressed:** `_csv_cell` (`app/export.py`) prefixes a leading apostrophe, the
conventional "this is text" marker. A leading `-` is exempted when the rest parses
as a number, so negative amounts are not defaced in every export to stop `-2+3`.

### R19 — the rate limiter believed the caller about who they were · P2, closed 2026-10-29
`_client_key` (`app/ratelimit.py`) always took the first hop of `X-Forwarded-For`
as the client identity. Nothing in front of the app sets that header, so a caller
could send a different value per request and never share a bucket with themselves:
`RUN_LIMIT_PER_MINUTE` was a limit on well-behaved clients only.

**Addressed:** the header is read only when `TRUSTED_PROXY` is on -- the
deployment's statement that a proxy it controls writes it. Otherwise the peer
address is the identity, because that is the one part of a request the caller
cannot choose. A supplied API key still wins over either, unchanged: a shared key
is one budget by design.

### R20 — the export that leaves the building had no policy of its own · P2, closed 2026-10-29
The served pages get a `Content-Security-Policy` header. The standalone report gets
none: it is saved, mailed, opened from a desktop, so there is no server to send a
header. Escaping (`esc()` on every interpolation, R16) is the actual control; this
is the second lock, for markup a future editor adds without escaping it.

**Addressed:** the artifact carries a meta policy in its `<head>` --
`default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none';
form-action 'none'`. The report has no script, no external style and no external
image, so a strict policy costs it nothing; the test asserts no directive
re-permits scripts, because `default-src 'none'` is the whole point.

### R21 — a header that is not ASCII produced a 500 before authentication · P2, closed 2026-10-29
`hmac.compare_digest` raises `TypeError` on a non-ASCII `str`. Headers are decoded
from raw bytes and a JSON body carries any Unicode at all, so a request with an
accented character in `X-API-Key`, in a bearer token, in the session body or in
`X-Config-Token` crashed the check instead of failing it
(`app/auth.py:81`, `app/config_view.py:42`, `app/main.py:176`). That is an
unhandled traceback anyone can provoke, inside the code whose job is to decide
whether to trust the caller.

**Addressed:** `secret_matches()` (`app/auth.py`) compares UTF-8 bytes through the
same constant-time routine, so the comparison stays constant-time and "nearly
right" is still indistinguishable from "wrong". `surrogatepass` because a JSON
string can carry a lone surrogate, which plain UTF-8 encoding refuses.

### R22 — nothing had ever pruned a cache · P2, closed 2026-10-29
`purge_older_than` covers the runs directory and nothing walked the response cache,
so a live instance accumulated every model answer it had ever received. (Runs are
still pruned only at startup -- that is the existing design, unchanged.)

**Addressed:** `LIVE_CACHE_TTL_HOURS`, default 0 (keep everything, like
`RUN_TTL_HOURS`), pruned at startup by `purge_cache_older_than`. The committed
fixture tree is refused by name: one script points its write directory at that tree
on purpose, so a TTL set on such a machine must be a no-op rather than a deletion.

### R23 — `/api/health` described the deployment to anyone who asked · P2, closed 2026-10-29
Health is open by design so a load balancer can poll it without a secret, and it
answered with the version, the git commit, both provider hostnames, the model ids,
the retention window and the model-selection record. That is a description of what
to target and what to imitate, handed to strangers.

**Addressed:** on a keyed deployment a caller without a key or session gets
`status`, `mode`, `demo_mode` and `api_protected` -- what a load balancer and the UI
actually need. The rest requires the same key as every other route. An open
deployment (the keyless demo) is unchanged, because nothing is being protected
there. The UI's two detail renderers now tolerate the short payload; it re-polls
after a session opens, which is exactly when the names mean something.

### Still deferred
- **Transitive dependencies float at image build**; `requirements.lock.txt` pins the
  direct ones. Pinning the transitive set needs `pip-compile` against a live index,
  which this environment cannot reach, so a refreshed lockfile here would be a
  guess dressed as a lock. It wants a machine with network access and a reviewable
  diff.

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
- **No `docs/`, no CI, no CHANGELOG, no ADRs** *(partly overtaken: `docs/` and a two-job CI workflow exist now; the CHANGELOG and the ADRs do not)* — decisions like "two providers",
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
| 7 | Auth or default-off for `POST /api/config` | P0 | **done** — `CONFIG_TOKEN` gates GET and POST; see `tests/test_config_token.py` |
| 8 | Done-callback and exception logging on the background task | P1 | B4 |
| 9 | Model discovery instead of hardcoded literals | P1 | **done** — `app/discovery.py`; logic verified against replayed catalogs, **unverified live** (no key). Export's duplicate literal removed |
| 10 | Nebius adjudicator for the 70-89 band and low-confidence cases | P1 | **done** — `app/adjudicate.py`; opinion cannot change a verdict, asserted by `tests/test_adjudicate.py`. Deterministic customs-risk strip and a guarded draft email added alongside. **Unverified live** (no key) |
| 11 | Streaming upload limit + magic bytes + PIL bomb limit | P1 | R2, R7 |
| 12 | Off-loop ingest and file IO | P1 | R3 |
| 13 | Lockfile, runtime/dev split, CI matrix | P1 | R8 |
| 14 | Eval harness and `docs/EVAL.md` | P1 | Phase 3 |
| 15 | Probe race, `_reports` eviction, `runs/` TTL | P2 | R4, R5 |
| 16 | Structured logging + IDs; docs/ set; ADRs | P2 | Phase 5 |
| 17 | Close the traversal at `store.dir()`, not at the route | P0 | **done 2026-10-29** — R10 |
| 18 | A live instance cannot boot without `API_KEYS` | P0 | **done 2026-10-29** — R11; `ALLOW_OPEN_LIVE_DEPLOY=1` to opt out |
| 19 | Gate `/retry` the way the upload is gated | P1 | **done 2026-10-29** — R12 |
| 20 | Reserve projected spend at dispatch | P1 | **done 2026-10-29** — R13. `cap_reached()` kept verbatim |
| 21 | Bound a PDF render by the page size it declares | P1 | **done 2026-10-29** — R14 |
| 22 | Split cache reads from cache writes | P1 | **done 2026-10-29** — R15. CI blocks on a dirty `fixtures/` |
| 23 | Test the standalone report's escaping | P2 | **done 2026-10-29** — R16 |
| 24 | A filename cannot put control characters into a label | P2 | **done 2026-10-29** — R17 |
| 25 | Neutralise CSV formula injection in the export | P2 | **done 2026-10-29** — R18 |
| 26 | Rate-limit identity behind `TRUSTED_PROXY` | P2 | **done 2026-10-29** — R19 |
| 27 | A CSP in the standalone report | P2 | **done 2026-10-29** — R20 |
| 28 | Auth comparisons on UTF-8 bytes | P2 | **done 2026-10-29** — R21 |
| 29 | Opt-in live-cache pruning | P2 | **done 2026-10-29** — R22 |
| 30 | `/api/health` detail behind a key on a keyed instance | P2 | **done 2026-10-29** — R23 |
| 31 | Pin the transitive dependency set | P2 | **deferred** — needs `pip-compile` against a live index |

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

### 2026-10-29 — the six fixes above

Two rows in the table directly above are now historical: that container booted live
with no `API_KEYS`, answered `GET /api/config` at 200, and served `/api/health` with
`demo_mode: false` and both `key_present: false`. The first of those is no longer a
configuration you can start with, and the second is no longer unauthenticated.

| Check | Command | Result |
|---|---|---|
| Suite before these changes | `pytest tests/ -q` | **1124 passed** |
| Suite after | `pytest tests/ -q` | **1158 passed in 75.62s** — 34 added, none removed or weakened |
| Coverage after | `coverage run -m pytest tests/ -q && coverage report` | **92%** (floor is 88) |
| Traversal, at the chokepoint | `RunStore.dir()` against the original repro | `..`, `../../etc/passwd`, `.`, `a/b`, `x`×25 all raise `ValueError`; `exists("..")` is `False` rather than an error |
| Traversal, over HTTP | `uvicorn` + `curl --path-as-is` | `/page/../.env`, `/page/%2e%2e/.env`, `/page/..%2F..%2F.env` → **404**; a real `/page/<id>/pages/doc1_1.jpg` → **200** |
| Malformed ids everywhere | `test_no_route_answers_a_malformed_id_with_a_500` | six routes plus two POSTs answer 404, not 500 — validation at the chokepoint must not leak a traceback |
| Reservation race | `test_n_uploads_cannot_all_pass_an_empty_ledger` | 6 concurrent uploads against a day sized for 2 → **2× 201, 4× 429**, ledger empty throughout. **Red without the reservation**: all six return 201 |
| Refused upload reserves nothing | `test_a_refused_upload_reserves_nothing` | one outstanding key, not five — a rejected attempt cannot lock the instance by trying hard |
| `/retry` gate | `test_a_retry_is_refused_when_the_day_has_no_money_left` | 429 with `Retry-After`, document status unchanged. **Red without the gate**: the refusal escapes `create_shipment` as an unhandled 500 |
| Boot gate, refused | `DEMO_MODE=false API_KEYS= uvicorn app.main:app` | `RuntimeError: refusing to start in live mode with API_KEYS empty`, naming `API_KEYS`, `DEMO_MODE=true` and `ALLOW_OPEN_LIVE_DEPLOY=1` as the three ways out |
| Boot gate, allowed | live with `API_KEYS` set | `/api/health` 200; unauthenticated `/api/config` **401** |
| Boot gate, opted out | `ALLOW_OPEN_LIVE_DEPLOY=1` | boots and answers 200 — the escape exists so it is never the default |
| PDF render clamp | `test_a_page_that_declares_a_ludicrous_size_is_never_drawn_at_full_scale` | asserts the scale handed to `render`, not the resized page that comes back. **Red when the clamp is removed from the call site** |
| Render deadline | `test_a_pdf_that_takes_too_long_to_draw_is_stopped` | moved clock through a seam in `ingest`, so it measures the guard rather than the scheduler |
| Live run vs tracked fixtures | `test_a_live_run_leaves_the_tracked_fixture_tree_byte_identical` + `git status --porcelain fixtures/` | **empty**. Reverting the write split makes the test fail and leaves one file in `fixtures/responses/` — observed, then removed |
| Escaping test is not vacuous | delete the `esc()` around the raw-value cell, `app/export.py:147` | **3 of 4 payloads fail**, then restored |
| Lint | `pyflakes app scripts tests` | 0 findings |
| Lint | `ruff check app scripts tests` | 0 findings (no `--fix`, no `ruff format`, per the repo's own rule) |
| Docs agree with code | `pytest tests/test_docs_freshness.py -q` | **12 passed** |


### 2026-10-29 — the P2 batch (R17–R23)

| Check | Command | Result |
|---|---|---|
| Suite after the second batch | `pytest tests/ -q` | **1186 passed in 81.83s** — 27 added, none removed |
| The offline-guard flake, reproduced | `test_closing_a_socket_also_revokes_the_port_it_authorized`, before the fix | `TimeoutError` at `tests/conftest.py:104` — the exact failure seen once in a full run, now deterministic. The guard authorized any port the process had *ever* bound; this Windows box times out rather than refusing a dead loopback port, which is why the symptom was a timeout and not a connection error |
| Guard after the fix | same test | refused by `_check_address` before a socket is created; the port is forgotten when the socket closes |
| Non-ASCII auth, four paths | `test_api_auth.py`, `test_config_token.py` | all 401. Before: `TypeError: comparing strings with non-ASCII characters is not supported`, from inside the check |
| CSV formulas | six payloads | each written with a leading apostrophe and still readable; `-1,234.00` left alone |
| XFF identity | two tests | peer address when `TRUSTED_PROXY` is off, first hop when on, API key always wins |
| Standalone CSP | one test | policy present in `<head>`, `default-src 'none'`, no directive re-permitting scripts |
| Cache pruning | four tests | old removed, fresh kept, committed tree refused by name, `0` inert |
| Health on a keyed instance | three tests | four fields to a stranger, the rest to a key holder, unchanged on an open instance |
| JS after the render guards | `node --check web/app.js` | parses |
| Lint | `pyflakes app scripts tests`, `ruff check app tests scripts` | 0 findings each |
