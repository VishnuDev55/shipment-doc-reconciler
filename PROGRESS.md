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
| 10 | Tests green, zero live calls in CI | ✅ | Offline suite green (count is not pinned in prose); **enforced in-repo** by the socket guard in `tests/conftest.py` (blocks every non-loopback connect and resolution), itself covered by `test_offline_guard.py`. The delivered repo had no mechanism behind this claim |
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

**What is still unverified.** *(written 2026-10-02 before any key was in hand;
superseded by the Phase 2 entry below.)* At that point discovery, consensus
against real endpoints, adjudication replies and the summary path were exercised
only against cached captures or mocked transports.
**CI is written but has never run** — the repo still has no remote.

**Bugs the tests caught before shipping:** a `.value` call on a plain float in the
adjudication prompt (hidden behind a broad `except`, which is why prompt
construction now sits outside the guard), and a presence check on
`NormalizedField.present`, an attribute that does not exist.


---

## Phase 2 — the field set, and the evidence that had to be rebuilt — 2026-10-02

Six fields became ten: `net_weight`, `incoterms`, `po_number`, `country_of_origin`.
Not for feature count — a reconciliation tool that cannot see Incoterms or origin
is blind to two of the things a customs entry actually declares. The work was
mostly keeping the *evidence* honest while the contract moved underneath it.

**One place decides the list.** `app/fields.py` now generates the JSON Schema, the
prompt, the normalizer dispatch, the comparator table, the scoring weights, the UI
rows and the authored fixtures. `scripts/selfcheck.py` was extended to assert an
expected verdict for **all ten** fields per fixture shipment and to fail if a
field has no recorded expectation, so the next addition cannot ship with the demo
quietly ignoring it.

**A field that is not printed is a field that is absent forever.** The four new
fields had to be rendered onto the synthetic documents (invoice: country of
origin; packing list: country of origin; bill of lading: net weight), which
changed every page hash, which invalidated all 27 bundled cache entries. So the
fixtures were rebuilt and **18 live extractions re-captured** — 9 per provider,
both keys, `prompt_version extract-v3`.

**Discovery was reading a field that does not exist.** Token Factory puts its
capability string at `architecture.modality`; `parse_catalog` looked for a
top-level `modality`. Result: 0 of 25 Nebius models appeared image-capable, and a
host that *states* its capabilities was being decided by a name guess. Measured
after the fix: 25 models, 25 declaring, 6 image-capable, and the retired
Nemotron vision model now resolves to `openbmb/MiniCPM-V-4_5` as a declared
capability. The tests were part of the problem — they asserted payload shapes
neither host returns, so the suite was green while the live path found nothing.
They now use the captured shapes, and a `text->embedding` row is pinned as
ineligible for both roles.

**The capture tool was capable of lying.** `scripts/live_compare.py` grew a
`--seed-cache` flag that pointed the client at the bundled cache. The cache falls
back to a hand-authored fixture matched on page hashes alone, so all nine
"live" calls were replays — and the script still wrote them out stamped
`LIVE API response, captured by scripts/live_compare.py`. It also counted loop
iterations and printed "9 live calls made". Three fixes: an outcome is only
captured when `source == "live_api"` and `used_cache` is false; the counter
counts real calls; a run that captures nothing exits non-zero. Nine files of
mislabelled evidence were deleted before anything was committed.

**A second, subtler one:** `load_authored()` matched cache entries on page hashes
without checking provenance, and the same directory holds *live captures of those
same pages*. So the authored baseline was whichever filename sorted first —
usually the other provider's model output. A diff line claiming
`country_of_origin: authored=True` on a document that never prints it was the
tell. Fixed, and `--diff-only` added so a comparison bug can be re-checked
without re-billing eighteen extractions to find out whether the data changed or
the tool did.

**What the two providers actually produced**, against the corrected authored
baseline: **NVIDIA 1 field difference, MiniCPM-V 7.** The seven are the same two
systematic mislabels on every shipment — a packing list's `INVOICE REFERENCE`
returned as the PO number, and `China` read out of the bill of lading's *port of
loading* as the country of origin — plus one dropped `FOB NINGBO` prefix that
normalizes to the same money key anyway. Both hints were rewritten to name those
exact traps. **It did not change MiniCPM's output**, so it is recorded as a model
limit rather than presented as fixed. The engine does the right thing with it:
the disagreement is reported with the printed evidence line attached, and the
score moves 15 → 25 on the clean fixture, which is the honest outcome — a wrong
extraction is allowed to look wrong.

**New deterministic rules and comparators.** `net_above_gross` (net mass above
gross mass is impossible, not merely inconsistent — and it only holds because
both sides were converted to kg first). Three comparators — `incoterm`,
`reference`, `country` — deliberately exact, with no fuzzy band: two PO numbers
90% similar are two different orders.

**Two normalizer bugs found by writing their tests.** `normalize_reference`
deleted bracketed content, so `PO No. (4410-DE)` lost the entire identifier and
returned no key; and `P.O. 4410-DE` hashed differently from `PO 4410-DE`, which
is a false conflict on the same order. `Made in P.R.C.` also had no mapping and
fell through to the verbatim branch — now it resolves to `CN`, verified against
real model output reading a real rendered page.

**Two things that could only rot.** The UI kept its own copy of the field labels,
so four of ten rows would have rendered `country_of_origin` at the user; the
labels now come from the report, which is the server's own contract. And
`migrate_cache_keys.py` would re-key a six-field capture onto a ten-field key
(reading as a complete response) and stamp hand-authored fixtures with a provider
they never had; it now refuses across a contract change and leaves authored
entries alone.

**Also:** the prompt said "extract the six fields" in two places after the set
grew — now derived from `len(FIELD_ORDER)` so it cannot go stale, and the
last-resort ladder rung names every field explicitly, because a field nobody
asked for comes back as `present=false` and reads as "not printed" rather than
"not extracted". `tests/test_prompt.py` pins that, and
`tests/test_normalize_categorical.py` covers the three new normalizers.

**Measured:** the offline suite green, zero network calls (socket guard), lint
clean, `scripts/selfcheck.py` passing with all ten fields asserted per fixture,
and the UI checked in a browser at 390 and 1280 px — ten rows, correct labels, no
snake_case leaks, no page-level overflow, and a new field's correction round-trip
(edit → normalizer applied → audit trail naming what the model said → revert).

**Still unverified:** CI has never run (no remote), and no hosted deployment was
made from this machine.


---

## Phase 2B — PDF report and a keyed HTTP API — 2026-10-03

**`app/pdf.py` renders from the Report model, not from the web page.** The UI
leans on CSS xhtml2pdf silently drops (grid, flex, custom properties), so
"print the HTML" would have produced a mangled artifact that happened to contain
the right words. This is tables all the way down: verdict and why, every field
with each document's verbatim value beside its canonical form, the risk score
with every contribution, cross-field findings, the corrections trail, and the
prompt/normalizer/comparator versions that produced it. Served at
`GET /api/shipments/{id}/report.pdf` and included in the export ZIP — where a
failed render is disclosed in `README.txt` rather than left as a file the reader
assumes was never promised.

**Two honesty bugs found by reading the PDF's own text layer back.** The
provenance line said "extracted by a live model call made during this run" while
`live_calls` was 0 — `result_source == "live"` means *model-produced*, not
*model-produced now*, because DEMO_MODE replays captures. And the value column
flagged every legitimately absent field as "low confidence 0.00", which is what
absence costs, not uncertainty. Both now read the fields that actually carry the
claim. A third was cosmetic but visible: `→` has no glyph in the renderer's
built-in Courier and came out blank in the one column an auditor checks by eye,
so the arrow is ASCII.

**`API_KEYS` turns the app into a keyed service.** Comma-separated, blank means
open (the keyless demo stays as it was). Any configured key is accepted via
`X-API-Key` or `Authorization: Bearer`, compared in constant time, and never
echoed in a response — a 401 that repeats the caller's key would put it in
somebody's access log. `/api/health` stays open so a load balancer can poll
without a secret, and it reports `api_protected` so the UI knows to ask.

**Query-string auth was deliberately not implemented**, even though it would
have made the download links trivial: a URL is logged by proxies, kept in browser
history and leaked in `Referer`. That decision forced the next one — a browser
cannot put a header on an `<img>`, an `EventSource` or a download, so
`POST /api/session` trades a key for a short `HttpOnly; SameSite=Strict` nonce
cookie and the key is then forgotten. Sessions live in process memory on
purpose: a cookie that survived a restart would be a credential nobody could
revoke.

**The offline guard had a hole, and it was not in a test.** While running the
suite, `test_a_live_mode_client_cannot_escape_to_the_network` failed: a
live-mode client had completed a real HTTPS request to
`integrate.api.nvidia.com` and received HTTP 200 — from inside the guarded
suite. The reason was the guard's own design. It allowed loopback ("so a test can
bind an ephemeral port"), and something on this machine listens on 127.0.0.1 and
forwards to the internet. The request never resolved a hostname through
`socket.getaddrinfo` and never connected to a non-loopback address, so the wall
was waved through while the guarantee was false.

Refusing loopback outright took out every API test: Starlette's `TestClient`
starts an anyio blocking portal, and on Windows that portal *is* a loopback
socket pair. The distinction that matters is not the address but who opened it,
so `conftest.py` now records every port the process binds and permits a
connection only to one of those. An external listener on loopback is refused on
the same grounds as a remote host. Resolving a loopback *name* is still allowed,
because that puts nothing on a wire and asyncio does it for its own plumbing.
Pinned as `test_a_loopback_port_this_process_never_bound_is_refused`.

**Verified:** 655 tests, lint clean, selfcheck green. Against a live instance
started with `API_KEYS` set: health open and reporting `api_protected: true`,
401 with no key and with a wrong key, 200 with either configured key and with
`Bearer`, no key material in any 401 body, session issued/accepted/revoked, the
evidence route 200 with a session and 401 without, the report finishing with 10
fields, and `report.pdf` returning 12,869 bytes of `%PDF-` — also present inside
the export ZIP. In the browser: the unlock panel appears on a protected
instance, the key opens a session, the fixtures and a 10-row report render behind
it, and the key field is cleared from memory afterwards.

**Not verified here, stated plainly:** the browser's own *download* of the PDF
could not be observed on this machine — the local interception layer answers
`204` to binary fetches, and it does the same to the pre-existing ZIP link, so
the failure is the environment rather than the route. The server side of both is
verified above.


---

## Phase 2 tail: goods-line matching, behind a flag that defaults off

**What.** `app/lineitems.py` matches the rows of a goods table across documents,
and the report gained a `line_items` section. The question it answers is the one a
clerk actually asks on the phone: not "do the totals agree" but "which line is
different". Both papers can print 100 cartons, 12,450 kg and the same value while
one lists 60 + 40 and the other 50 + 50. Every field comparison passes that
shipment.

**The design choice that matters.** Nothing is scored on similarity. Two rows are
the same row when they carry the same item number, or when both carry none and
their descriptions fold to the same token set — case, accents, punctuation, word
order and package words removed, every number kept. A `token_set_ratio` band
anywhere in here would score `H-40` against `H-41` as a near match and delete the
only finding in the room. The cost is accepted knowingly: `ALUMINIUM` and
`ALUMINUM` fold apart and appear as two rows, and there is a test asserting
exactly that, because two rows a person can reconcile beats one row that quietly
merged.

**Three defects found while building it, all caught by tests rather than review.**

- `match_line_items` credited a document to only its *first* row. The guard
  against double-applying a document was a per-document set shared across every
  match, so the second and third rows of a page had an empty `documents` list —
  and `_classify` then indexed `documents[0]` and raised `IndexError`. The
  pipeline test hit it; the unit tests as first written did not, because most
  used one row per document.
- The authored-fallback cache tier matches on page hashes alone, so a goods-lines
  fixture for a page would have answered a *ten-field* lookup for the same page.
  It would not have raised: the shape parses, every field comes back absent, and
  the report reads as a document that states nothing. The tier now filters on
  `prompt_version`, which meant `ResponseCache.put` had to stop hard-coding the
  scalar tag.
- A line present on one document and absent from a document that was never read
  for lines was described as "the other document lists no such item". That is a
  claim about the bill of lading's contents, and nobody looked at it. Documents
  that returned no rows are now named as not participating, separately from
  documents checked and found not to print the line.

**Wiring.** `ENABLE_LINE_ITEMS=false`, a second contract tag
(`lineitems-v1`) rather than a second cache directory, a strict-mode schema whose
example stays un-copyable for the reason `example_output()` documents, one
correction rung and no ladder (a failure here returns a reason, because the
scalar report is already correct without it), and the orchestrator writing the
section *after* the report has been settled and persisted.

**Verified.** Offline suite green, pyflakes and ruff clean, selfcheck green. The
default-off path is pinned by running the same fixture shipment twice and
asserting the comparisons, summary, risk note, sanity findings and every document
dump are identical with the flag on and off. End to end through a running server
with a scratch cache: the JSON carries the conflict with both printed quantities
verbatim, the browser panel renders it with the row tinted and the provenance flag
reading `hand-authored fixture — no model read these lines`, and the shipment
verdict stayed `clean` while a line conflicted — the exact case the feature
exists for. The PDF section prints both documents' strings and refuses to render
when the feature was never asked for.

**Not verified, stated plainly:** no live model has ever produced goods lines for
these fixtures, so the feature's *accuracy* is unmeasured and `DEMO_MODE` reports
`skipped` rather than showing an empty table. The scratch cache used for the UI
check was deleted; nothing hand-authored was written into `fixtures/`, where it
would have become a fake model result.
