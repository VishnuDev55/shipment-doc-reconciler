# PROGRESS — Shipment Doc Reconciler

Nebius × NVIDIA Global AI Hackathon · solo · deadline ~2026-10-30.

**Status: submission package complete on my side, live-verified on BOTH
providers.** Extraction runs on NVIDIA build.nvidia.com; the executive summary
runs on Nebius Token Factory. Four items need you — see SUBMISSION CHECKLIST.
Two are platform facts rather than code problems: the brief's exact model is
unreachable on both keys, and neither a `docker build` nor a hosted deployment
could be executed in this sandbox.

---

## Compliance: how each hackathon requirement is met

| Requirement | Met by | Verified |
|---|---|---|
| Runs on **Nebius Token Factory** | Executive summary: runtime `POST https://api.tokenfactory.nebius.com/v1/chat/completions`, model `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, one call per shipment | ✅ live, HTTP 200, clean prose |
| Uses an **NVIDIA open-source model** | Vision extraction: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` via build.nvidia.com. The summary model is *also* an NVIDIA open model (Nemotron), hosted by Nebius | ✅ live, 9/9 documents |

**The split is explicit because it is not obvious from the code:** the primary
function (extraction) is on build.nvidia.com; Nebius Token Factory carries the
reasoning/summary feature. `VISION_PROVIDER=nebius` moves extraction to Nebius
too — that path is also live-verified, using `openbmb/MiniCPM-V-4_5`.

---

## SUBMISSION CHECKLIST — every hard requirement

Legend: ✅ done here · ⚠️ needs you · ❌ blocked

| # | Requirement | Status | Where / what |
|---|---|---|---|
| 1 | **LICENSE** (MIT), named so GitHub auto-detects | ✅ | `LICENSE` at repo root, standard MIT text, `Copyright (c) 2026 …` |
| 2 | README states "Runs on Nebius Token Factory" near top, with exact model | ✅ | README §"How this meets the two hackathon requirements" — first section after the title. Names `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` and the full endpoint URL |
| 2b | README states "uses ≥1 NVIDIA open-source model", exact IDs, no paraphrase | ✅ | Same section — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` (extraction) + the Nemotron summary model |
| 2c | Two separate provider calls doing two jobs, explicit | ✅ | Same section, "Division of labour, explicitly" + a table |
| 2d | Setup: two keys, where to get each, .env shape | ✅ | README §Quick start → "Running against the live APIs": both signup URLs, a table, and a full `.env` listing |
| 2e | MiniCPM-V documented as optional fallback, not default | ✅ | README §"Optional: run vision on Nebius instead" + `VISION_PROVIDER` in `.env.example` |
| 3 | Both calls happen at **runtime** on the running app | ✅ code / ⚠️ hosted instance | Implemented as runtime calls; each verified live. **Cannot claim the hosted demo makes live calls** — see honesty note below |
| 3b | Honest about DEMO_MODE replaying captured output | ✅ | `RunInfo.result_source` + `live_calls`; UI and exported HTML both state it. A cached *authored* value can never be reported as live |
| 4 | **Working hosted demo URL** | ⚠️ **needs you** | Cannot deploy: no Render/Railway/HF account or token in this sandbox. `Dockerfile` + `render.yaml` are ready; steps in README §"Demo / deployment status" |
| 5 | **Public GitHub repo** | ⚠️ **needs you** | No git credentials here. The delivered folder had **no `.git` at all**; it was initialized locally on 2026-10-02 and committed (121 files, secret-scanned). Needs your GitHub username + repo name + auth |
| 6 | SUBMISSION.md (description, pain point, how it works, track, newly-created statement) | ✅ | `SUBMISSION.md` — all six parts present |
| 6b | Track named explicitly | ✅ | **Best Apps and Agents**, with reasoning for why it still fits the two-provider build |
| 6c | "Newly created during the submission period" stated | ✅ | SUBMISSION.md §"Newly created during the submission period" |
| 7 | Platform feedback, specific not generic | ✅ | SUBMISSION.md §"Feedback on the Nebius and NVIDIA platforms" — 7 items, each with the actual error text |
| 8 | VIDEO_SCRIPT.md, ≤3 min, covers how both providers were used | ✅ | `VIDEO_SCRIPT.md` — 8 shots, timings from **measured** latency, real captured summary text, honesty checklist |
| 9 | `docker build` verified | ⚠️ **needs you** | No docker/podman binary or daemon in this sandbox. Dockerfile audited statically (see below) and the app runs under its exact `CMD` locally — but the build itself has never executed |
| 10 | Tests green, zero live calls in CI | ✅ | 297 passed; **enforced in-repo** by the socket guard in `tests/conftest.py` (blocks every non-loopback connect and resolution), itself covered by `test_offline_guard.py`. The delivered repo had no mechanism behind this claim |
| 11 | No API key committed or packaged | ✅ | Scanned all 119 tracked files and every zip entry for both key values and any assigned `*_API_KEY=` line: none |

### Two things I want to be precise about, not optimistic

**Requirement 3 (runtime calls).** The code makes real runtime calls to both
providers and I verified each one live. But I have **not** deployed an instance,
so I cannot honestly claim "the hosted demo calls Nebius on every visit" — and
the recommended public config is `DEMO_MODE=true`, which deliberately does not.
What is true: the Nebius call is a genuine runtime code path, verified against
the live API, and the app states per-report whether a value came from a call
made *this run* or from a previously captured response.

**Requirement 9 (docker).** I did **not** build the image. What I did instead,
since "should be fine" is not verification:
- Confirmed no container tooling exists here (`docker`, `podman`, `buildah`,
  `nerdctl`, `ctr`, `kaniko`, `colima` all absent; no `/var/run/docker.sock`).
- Downloaded all **69** transitive deps as cp311/manylinux_2_28 wheels — every
  one a binary wheel, none an sdist, so nothing compiles inside the image.
- Extracted them and read `DT_NEEDED` across **210** shared-object edges,
  checking each against the `manylinux_2_28` system-library whitelist from
  `auditwheel`'s own policy file.
- **Pillow specifically, since you asked me not to assume:** it needs
  `libjpeg`, `libtiff`, `libopenjp2`, `libwebp`, `libfreetype`, `libharfbuzz`,
  `liblzma`, `libzstd`, `libxcb` — and **vendors all of them** inside
  `pillow.libs/`, resolved via `RPATH: $ORIGIN/../pillow.libs`. Its one real
  system dependency is **`libz.so.1`**, which is on the manylinux whitelist and
  is linked by `apt` itself, so any Debian image that can run `pip` has it.
  **No `apt-get install` is needed.** `pypdfium2`'s bundled `libpdfium.so` needs
  only libc/libm/libgcc_s. `rapidfuzz` needs `libstdc++.so.6` (whitelisted).
- Ran the app under the Dockerfile's exact `CMD` with no `.env` present:
  `/`, `/api/health`, fixture run and ZIP export all returned 200.

**Result: zero unresolved system libraries.** That is a static audit, not a
build — a real `docker build` could still fail for a reason this can't catch
(base-image pull, layer caching, uid/gid assumptions in `useradd`).

### NEEDS ME — exact commands

**1. Verify the Docker build (do this first — everything else depends on it):**
```bash
cd shipment-doc-reconciler
docker build -t shipment-doc-reconciler .
```
A **successful** build ends with:
```
Successfully tagged shipment-doc-reconciler:latest
```
and `docker images | grep shipment` lists it. Expect ~10 pip layers, no
`apt-get`, and a final image size roughly 400–600 MB. A **failure** worth
reporting back to me verbatim: any `error while loading shared libraries`
(audited against, but this is the exact class of thing a static audit can miss),
or a `pip` resolution error mentioning `--platform`/wheels.

Then confirm it actually serves:
```bash
docker run --rm -p 8000:8000 shipment-doc-reconciler
# in another shell:
curl -s localhost:8000/api/health
curl -s localhost:8000/api/fixture/shipment_conflict
```
`/api/health` should return `"status":"ok"` with `vision.provider: "nvidia"` and
`summary.provider: "nebius"`.

**2. Public repo** — tell me the GitHub username and repo name, or run:
```bash
cd shipment-doc-reconciler
git commit -m "Shipment Doc Reconciler: two-provider trade document reconciliation"
git branch -M main
gh repo create <owner>/shipment-doc-reconciler --public --source=. --push
# or without the gh CLI:
git remote add origin https://github.com/<owner>/shipment-doc-reconciler.git
git push -u origin main
```
Already staged and verified secret-free; `.env` is gitignored.

**3. Deploy** — after the repo is public: Render dashboard → **New → Blueprint**
→ pick the repo. `render.yaml` is read automatically. Or import the Dockerfile
manually as a **Web Service**, plan **Free**, and set env vars
`DEMO_MODE=true`, `RUNS_DIR=/tmp/shipment-runs`. Then send me the URL and I will
test it end-to-end and record the result here.

**4. Decide the model question** — see REMAINING GAP below. Not revisited per
your instruction; listed only so the submission risk is visible.

---
---

## 🔴 REMAINING GAP — the brief's exact model is unavailable on both platforms

`nvidia/Nemotron-Nano-V2-12b-VL` cannot be called with either key:

```
Nebius Token Factory   GET /v1/models  -> 200, 24 models, no Nemotron vision model
                       POST /chat/completions model=nvidia/Nemotron-Nano-12B-V2-VL
                                            -> 404 "The model ... does not exist."
                       (all 4 nvidia/* models report "modality": "text->text";
                        sending an image -> 400 "This model does not support image input")

NVIDIA build.nvidia.com GET /v1/models -> 200, 82 models, no 12b-v2-vl entry
                       POST model=nvidia/nemotron-nano-12b-v2-vl
                                            -> 410 Gone: "has reached its end of
                                                life on 2026-08-26T09:00:00Z"
```

The NVIDIA documentation page for that model is **stale** — it still renders the
full model card and says "Updated about 2 months ago", with no mention of the
retirement. Only calling it reveals the 410.

**What I substituted and why:** `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`
is an NVIDIA open-source model on NVIDIA's own hosted API that takes image input.
It is the nearest reachable equivalent, chosen from that platform's own model
listing rather than guessed.

**Action needed from you:** if the judging criteria require that specific model
string, it has to be provisioned somewhere — neither key can reach it. If "an
NVIDIA open-source model" suffices, the current setup satisfies it on both
counts (Nemotron omni for vision, Nemotron Nano for the Nebius summary).

---

## Live verification — what was actually run

### NVIDIA build.nvidia.com (vision — the core pipeline)
- `GET /v1/models` → 200, 82 models. Confirmed the base URL by probing three
  candidates: `integrate.api.nvidia.com` (200), `build.nvidia.com/v1` (returns
  HTML, not an API), `api.nvidia.com` (TLS hostname mismatch).
- Capability probe → **strict `json_schema` honoured** (wrapped envelope).
- **9/9 fixture documents extracted.** 11 successful calls; every response
  parsed on ladder rung 1, so rungs 4–5 were never needed. Latency 30–50 s/page.
- **One document failed outright on its first invocation**: `bol_messy.png` hit
  five consecutive 503 `ResourceExhausted` responses and exhausted the transport
  ladder, reporting `transport failed`. Re-run later, it succeeded on one
  attempt. NVIDIA availability — not parsing — is the real reliability risk, and
  the failure mode was clean and diagnosable rather than a silent wrong answer.
- Two independent calls on the same page returned **byte-identical** extractions
  (both kept in `fixtures/live_responses/` as a determinism datapoint).
- No rate-limit, quota or credit headers are returned at all — spend visibility
  is dashboard-only, so calls were kept to the minimum.

### Nebius Token Factory (summary — the requirement)
- Capability probe → **strict `json_schema` honoured** (after the envelope fix).
- One live summary per shipment. Example, from `shipment_conflict`:
  > "2 fields agree and 2 fields need attention. Gross weight and container
  > number discrepancies need attention."
- Usage 605 prompt / 521 completion tokens ≈ $0.0002 per shipment.
- All three fixture summaries are captured into `fixtures/responses/narratives/`,
  keyed on verdict signature, so DEMO_MODE replays genuine model prose.

### Measured cost this session
~$0.0017 per 1-page Nebius extraction, ~$0.0002 per Nebius summary. NVIDIA
build.nvidia.com exposes no pricing or balance endpoint; the trial tier is
free-credit based. Total Nebius spend across both sessions is well under 10 cents.
Rate limit observed on Nebius for MiniCPM-V: 300 RPM / 200K TPM.

---

## Live-vs-authored diffs, adjudicated against the document images

Across 18 live extractions (9 NVIDIA + 9 Nebius) vs the hand-authored cache,
only a handful of fields differed. For each I checked the rendered page before
deciding who was wrong — I did not silently "fix" authored data to match the
model.

| Difference | Who was right | Action |
|---|---|---|
| BOL consignee: model `Nordwind Industrial Supply GmbH`, authored all-caps | **Model.** `td.pval` has no `text-transform`; the page prints mixed case | Fixed the fixture (2 places) |
| Packing-list `page_index`: model 2, authored 2 — but totals were on page 1 | **Neither.** My 2-page fixture never tested cross-page evidence | Rebuilt the fixture so totals genuinely land on page 2; model then correctly reports 2 |
| `13,200.000 KGS` (MiniCPM invented a digit, repeated it in `evidence_text`) | **Model wrong** | No code change; normalizer absorbs it → same `compare_key`. Raw value still displayed so the error is visible |
| `USD 8,420.00` vs `FOB NINGBO USD 8,420.00` | **Model less verbatim than instructed** | Both → `money:USD:8420.00`; no verdict change |
| NVIDIA captured `13,200.00 KGS` **correctly** where MiniCPM invented a digit | — | Generalized two tests that had pinned one model's quirk to a shared fixture |

---

## Bugs the live work exposed in my own code

1. **Both providers reject the `json_schema` envelope their docs show.**
   Nebius 422 `json_schema.name: Field required`; NVIDIA 400 `missing field name`.
   Both accept OpenAI's wrapped `{name, strict, schema}`. Client fixed; test pins
   the working shape.
   *Second-order effect:* my capability probe had been reporting "strict
   rejected" — a **false negative**, because strict was never fairly attempted.

2. **My prompt's shape example was being copied verbatim.** On a page headed
   `PACKING LIST`, the model returned `doc_type: commercial_invoice` with the
   example's `doc_type_reason` word-for-word. Fixed by making the example
   structurally complete but deliberately un-copyable, and by letting a user's
   declared type override the model's guess (recorded as
   `doc_type_disagrees`, never silently applied). Verified: a fresh live call on
   the same page now returns `packing_list`.

3. **A 503 was being interpreted as "mode unsupported."** NVIDIA's shared workers
   return `ResourceExhausted: Worker limit (16/16)`; the probe downgraded a model
   that had accepted `strict` minutes earlier. Capacity is now retried,
   capability is not.

4. **I then over-corrected.** My first fix nested a 3× probe retry inside the 5×
   NVIDIA transport ladder — up to 15 API calls and ~200 s of sleeps to answer
   one yes/no question, which is the brute-forcing I was told to avoid. Added
   `max_attempts` so the probe caps its own transport retries.

5. **Reasoning models don't populate `content` reliably.** Nebius Nano returned
   `content: null` with the answer in `reasoning`; NVIDIA returns both fields.
   Reading `.content` alone yields an empty extraction on HTTP 200. `_message_text()`
   handles all cases, and empty completions are now retried.

6. **The AI summary did arithmetic and got it wrong.** Asked how many fields
   "need attention", it counted the two single-source fields on a shipment with
   zero problems. The prompt now supplies precomputed counts; the model only
   phrases them.

7. **My plausibility guard missed truncated output.** A reasoning model that
   spends its budget thinking returned `"...need attention. Gross"` — no
   chain-of-thought markers, so every existing check passed. Now requires
   terminal punctuation and a substantive final clause.

8. **Adding `provider` to the cache key orphaned 9 real captured responses** —
   their filenames no longer matched the lookup key. Wrote
   `scripts/migrate_cache_keys.py` (idempotent, collision-safe); migrated 18,
   lost none.

9. **`build_fixtures.py` was about to destroy real data.** Its "clear stale
   output" step deleted every `*.json` in the cache directory, which by then also
   held live-captured responses and AI narratives. Now removes only entries
   marked HAND-AUTHORED.

10. **Placeholder API keys were being sent to the network.** My new client
    checked `api_key.strip()` (non-empty) instead of rejecting
    `your-api-key-here`, so an unconfigured `.env` copy produced a confusing 401
    rather than a clear local error. Caught by an existing test that started
    failing — `ConfigError` is now raised and deliberately not retried.

11. **My own test-suite blocker didn't cover the new provider.** After adding
    NVIDIA I reported "225 tests pass with the network blocked" — but the guard
    only listed Nebius hostnames, so the claim proved less than it sounded.
    Extended to all five NVIDIA/Nebius hosts and re-run.

---

## Honest notes on process

- **I corrected a fabricated number in my own log.** An earlier draft claimed
  "13 live calls" written without counting; the evidenced figure was 12.
- **My test expectations were wrong several times** (ISO 6346 hand-arithmetic ×2,
  cartons-vs-packages, `partial` vs `done`, `norm.raw` which doesn't exist, two
  `APIStatusError` constructor mistakes repeated across files). Fixed the test,
  not the code, and said so each time.
- **Two tests I wrote asserted nothing** (`x != y or True`). Deleted.
- **`find_authored_by_pages` was removed then re-added** when I found live tests
  depended on it.

---

## Verification status

| | Status |
|---|---|
| Normalizers, comparators, parse ladder | ✅ 297 unit tests |
| Pipeline, UI, ZIP export, HTTP API | ✅ cache-verified **and** live-verified |
| Vision extraction on NVIDIA | ✅ 9/9 documents; 11 calls, all rung 1 |
| Vision extraction on Nebius (fallback) | ✅ 9/9 documents, attempt 1 |
| Nebius summary call (the requirement) | ✅ live, 3 shipments |
| `json_schema` strict on both hosts | ✅ wrapped envelope only |
| Checksum arbitration on live output | ✅ flagged the planted bad container |
| Cross-page `page_index` | ✅ correct on live output |
| Extraction determinism | ✅ two independent calls byte-identical |
| Pricing / context / rate limits | ✅ live `?verbose=true` (Nebius) |
| Automated suite makes zero live calls | ✅ **enforced** — `tests/conftest.py` refuses every non-loopback socket, 297 pass |
| **`nvidia/nemotron-nano-12b-v2-vl`** | ❌ **410 Gone (NVIDIA), 404 (Nebius)** |
| `docker build` | ⚠️ never run — no Docker daemon in this sandbox |
| Judge scenario (no keys at all) | ✅ replay fires, arbitration works, provenance labelled honestly |

---

## What's left

Superseded by **SUBMISSION CHECKLIST** near the top of this file, which is the
single source of truth. Short version: four items need you — `docker build`,
the public repo, the hosted URL, and the model decision. Everything else is
done and verified. The demo video script is written (`VIDEO_SCRIPT.md`) but the
recording itself is yours to make.


---

## Appendix: the offline build (before any key existed)

Everything below was established without a live API, and still holds.

### Research findings from Nebius's own docs

| Fact | Value | Source |
|---|---|---|
| Base URL | `https://api.tokenfactory.nebius.com/v1/` | quickstart, api-reference/introduction |
| Auth | `Authorization: Bearer <key>` | api-reference/introduction |
| Vision part | `{"type":"image_url","image_url":{"url":…}}` — standard OpenAI schema works unmodified, http(s) URL or base64 | examples/vision-capabilities |
| Files endpoint | **batch JSONL only** — no document-upload path, so PDFs must be rasterised. Drove the pypdfium2 choice | examples/files |
| Context window | **128K** ("Input + Output Token: 128K", NVIDIA model card) | NVIDIA HF card |

**32K vs 128K vs 131K resolved:** 128K and 131K are the same window written two
ways (131,072 tokens = 128 Ki). The "32K" figure is unsupported by either
NVIDIA's card or Nebius's docs.

The model-ID guess was settled by the API, not by inference: Token Factory casing
is inconsistent across models (`nvidia/Nemotron-3_5-Lightning` vs
`nvidia/nemotron-3-super-120b-a12b`), so it cannot be pattern-derived. Every
unauthenticated route was exhausted first — `/v1/models` (401), catalog URLs
(client-rendered SPA), `llms.txt`, a 789 KB `llms-full.txt` dump, the UI JS
bundle, and legacy `docs.nebius.com/ai-studio/models` (404).

### Bugs found during the offline build (all caught by tests)

1. **Comparators received absent values** — a document where a field legitimately
   isn't printed reached `normalize_container("")`, reported "checksum FAILS" and
   manufactured a conflict on the *clean* fixture; also corrupted
   `majority_compare_key`.
2. **Ambiguous-number default inverted** — `"1,200"` parsed as 1.2 instead of the
   thousands convention (1200); cascaded into weight and quantity.
3. **Incoterm strip swallowed the amount** — `FOB[^,;]*` ate through
   "FOB SHANGHAI USD 8,420.00", losing number *and* currency.
4. **`ResponseCache.put()` kwarg mismatch** (`page_sha256` vs `page_sha256s`) —
   a guaranteed crash on the live path, unreachable in DEMO_MODE. Later exercised
   for real once live caching wrote entries.
5. **Cache keyed on the wrong artifact** — a 2-page document ingested as two
   separate files missed the cache and *silently dropped a document*.
6. **Low-confidence check scanned absent fields** (confidence 0.0), wrongly
   downgrading consistent results.
7. **`NameError` on an `except ... as e` binding** used outside the except block.
   Latent only — verified reachability rather than claiming a live crash.
8. **Path containment used `str.startswith`** — `runs/abc` is a prefix of
   `runs/abcdef`, so a sibling run was readable. Now `Path.is_relative_to`.
9. **`present=false` with a value survived validation** on the clean path. Now
   enforced in a `model_validator`, resolving toward "absent" (never invents data).
10. **`/api/health` read `get_settings()`**, not the injected reconciler.
11. **Fixture generator left orphaned cache entries** when templates changed
    (18 files for 9 documents).
12. **`result_source` broke report persistence** — I changed its return values
    without updating the `Literal` type, so `report.json` wrote but failed to
    reload. Caught by the round-trip test.
13. **Changing `NEBIUS_MODEL` silently broke DEMO_MODE** — the cache key included
    the model string, so repointing it missed every authored fixture. Authored
    fixtures now match on page hashes; live responses stay model-keyed.

### Offline verifications still worth citing

- **ISO 6346 checked against the standard's published worked example**, not just
  my arithmetic: `CSQU305438` → weighted sum **6185** → check digit **3**.
  Letter table A=10 B=12 J=20 K=21 U=32 V=34 Z=38 (multiples of 11 skipped).
- **Fixture determinism**: byte-identical PNGs and identical cache keys across
  rebuilds.
- **`make dist` → extract to a clean dir → `pytest`** passes from the zip.
- **Python 3.11 grammar parse** (Dockerfile pins 3.11-slim): no incompatible syntax.

### Deliberate deviations from the spec

- **`_compare_count` classifies package basis** (item / package / unknown). The
  spec's "different package kind → warn" cannot distinguish *pieces vs cartons*
  (benign) from *cartons vs pallets* (a real conflict).
- **Money tolerance added** — spec gives a weight table but none for money.
  Frozen in the same constants block: 0.01% consistent, 1% suspicious.
- **Multi-page fixtures ship as one PDF**, because the app uploads one file per
  document.
- **Normalizer functions imported by name** (`parse_number`, `normalize_weight`);
  the spec's `from app.normalize import container as norm_container` would have
  bound a module to a name the tests then called.
- **Two providers rather than one** — forced by the model being unavailable; see
  REMAINING GAP.

---

## Audit — 2026-10-02

Full findings with `file:line` evidence in **[`docs/AUDIT.md`](docs/AUDIT.md)**.
Read-only audit of the delivered repo, then Phase 0 fixes only; no feature work.

**Confirmed good, left alone.** The model still never does arithmetic;
tolerances are still frozen constants (`app/compare/__init__.py:25-36`);
provenance is still versioned; path traversal, ZIP handling and key masking all
checked out.

**Fixed.**

- **The offline guarantee was documented as enforced but was not.** There was no
  `conftest.py` and `tests/__init__.py` was empty. `tests/conftest.py` now
  refuses every non-loopback connect, `create_connection`, `getaddrinfo` and
  `gethostbyname` for the session; loopback stays open because `TestClient`
  reaches the app through an in-process `ASGITransport`. `test_offline_guard.py`
  keeps the wall itself under test, including a **live-mode** `ChatClient` built
  with a key that passes the placeholder check — that call now dies on the socket
  wall instead of reaching the provider. 286 existing tests pass untouched, 11
  added, **297 total**.
- **Every test count in the docs was wrong**, and README contradicted itself
  (286 in one place, 268 in three). `make dev` was described as live while
  `Makefile:27` pins `DEMO_MODE=true`. Corrected across README/SUBMISSION/PROGRESS.
- **`make lint` could not run on a fresh clone** — `pyflakes` was invoked but
  never declared. Declared; lint reports 0 findings.
- **The repo had no `.git` at all**, despite `PROGRESS.md:43` recording it as
  initialized and staged. Initialized; baseline commit first so every later
  change is a reviewable diff. No remote added and nothing pushed.

**Found, not yet fixed** (deliberately deferred to keep this phase reviewable):

- `runs/{id}/raw/` is always empty — the client is built without `raw_dir`
  (`app/orchestrator.py:268`), and per-attempt token/usage data is collected then
  discarded (`app/orchestrator.py:207-208`, `:500`). This is the prerequisite for
  any honest transparency panel.
- `POST /api/config` is unauthenticated and mutates process-global settings; the
  default is open (`app/config.py:65`). `render.yaml` mitigates, local does not.
- Uploads are buffered fully into RAM before the size check
  (`app/main.py:246-249`); rasterization and file IO block the event loop; the
  background task has no done-callback, so a late failure hangs the run at
  `running` forever.
- No CORS, security headers, rate limiting, `HEALTHCHECK`, or lockfile.

**Standing honesty note:** no API key was available during this audit, so nothing
on the "verified live" tables has been re-run since the original captures. Those
claims rest on the committed artifacts in `fixtures/live_responses/`, and
`README.md` now says so.

---

## Phase 1 — hackathon-critical features — 2026-10-02

Six items, each committed and verified separately. **391 tests pass** (286 at
baseline), lint clean, image builds and reports `healthy`.

**1.1a — keep the evidence a run already produced.** `ExtractionClient` was built
without `raw_dir`, so `runs/{id}/raw/` was empty in every production run while
`export.py` shipped it in the ZIP. Per-attempt token usage was collected into
`AttemptLog` and then discarded, and the summary's usage was bound to `_usage`
and lost. `raw_dir` is a per-call argument, not a constructor one, because the
server shares one client across every shipment. Cached bodies are written too,
while usage stays zero for them: zero tokens and zero evidence are different
claims. Verified in a real run — three ~950-byte bodies on disk.

**1.1b — transparency panel.** Provider, model, source, tokens and latency per
document, plus the raw bodies verbatim. A run that made no call says so in an
amber caveat; an authored fixture is never worded as model output; cost appears
only if `USD_PER_MTOK` is set, so the panel declines to invent a price. New route
`GET /api/shipments/{id}/raw/{name}` accepts only filenames the report listed.

**1.2 — model discovery.** `GET /v1/models` on each host before a run extracts;
if the configured name is gone, a served one is adopted and the report records
`model_selection: "discovered"`. This is the automated version of the check that
originally cost the project its model choice (410 Gone / 404, found by hand).
Discovery never invents a name, keeps a confirmed model so cache keys stay
stable, and cannot abort a run — it now catches any exception, because the first
cut caught only its own error type and a `TypeError` from a bad payload would
have sunk the shipment.

**1.3 — adjudicator, risk strip, draft email.** Nebius is asked only about the
two interpretive field kinds, and only where the engine already said
`suspicious`. The opinion is stored beside the verdict with no path to changing
it; tests snapshot every decided field before and after, including a model that
contradicts the engine and a document that tries to instruct the model to flip a
verdict. Customs risk is computed by rule and labelled `deterministic` — a risk
statement an auditor cannot trace to a rule is worth less than none.

**1.4 — SSE progress.** Fan-out hooks `RunStore.write`, the one place every
report mutation already funnels through. The browser keeps the 1s poll until the
first stream event proves the channel is alive, then drops it; if the stream is
blocked the behaviour is exactly as before. Listeners are bounded and drop their
oldest frame so a stalled client cannot stall a run.

**1.5 — optional second reader.** `CONSENSUS=true` has the other provider read
the same pages and compares on `compare_key` in deterministic Python. Disagreement
marks a field **needs review**; it does not tie-break. Off by default (roughly
doubles spend). Both sides are real captured output — the cache holds a NVIDIA and
a Nebius response for every fixture document — so it demonstrates offline without
inventing anything.

**1.6 — deploy.** `HEALTHCHECK` (stdlib, since `-slim` has no curl and the image
takes no `apt-get`), `docker-compose.yml` with `runs/` on a named volume,
`.github/workflows/ci.yml`, and `docs/DEPLOYMENT.md`. Health returns 200 even with
no key and reports provider readiness inside the body, deliberately: an
unconfigured container must not be restarted as a crashed one.

**What is still unverified.** Everything live. No key existed for any of the
above, so discovery, consensus against real endpoints, adjudication replies and
the summary path are all exercised against cached captures or mocked transports.
**CI is written but has never run** — the repo still has no remote.

**Bugs the tests caught before shipping:** a `.value` call on a plain float in the
adjudication prompt (hidden behind a broad `except`, which is why prompt
construction now sits outside the guard), and a presence check on
`NormalizedField.present`, an attribute that does not exist.
