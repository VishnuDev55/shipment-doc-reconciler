# Verification

What the tests cover, what the numbers were measured against, and what still has no evidence behind it. Read the status table last, not first: it is the table that says what is *not* verified.


## Tests

```bash
python -m pytest tests/ -q      # the whole offline suite, zero API calls
```

The suite prints its own count, and this file does not repeat it: a hand-typed
test total is a number that rots on the next commit. What is stable and worth
stating is *how long* and *why*: measured on the development machine, the offline
suite runs in about a minute, and the browser suite in a little over half that.
Most of it is CPU work rather than waiting — `test_pipeline`, `test_api`,
`test_severity` and `test_client` deliberately re-run the **real** image and PDF
ingestion path (~128 ms per page, ~730 ms for the 2-page PDF) instead of stubbing
it, so they prove the pipeline end to end rather than proving a mock. The slowest
single test call measured on 2026-10-23 was 2.0 s (`test_zip_export_contains_all_four_artifacts`);
the 7 s item in `--durations` output is the session-scoped fixture that builds the
first report, not a test. If the suite ever gets much slower than this, suspect a
network timeout sneaking past the guard, not the math.

### The offline guarantee is enforced, not documented

`tests/conftest.py` patches `socket.connect`, `connect_ex`,
`create_connection`, `getaddrinfo` and `gethostbyname` for the whole session.
Anything that is not loopback raises `AssertionError` naming the target. That
covers every provider host at once — `api.tokenfactory.nebius.com`,
`integrate.api.nvidia.com` and any future one — instead of relying on a list of
names to remember.

Loopback stays open because `TestClient` reaches the app through httpx's
in-process `ASGITransport`, which never opens a socket;
`test_offline_guard.py` pins both halves of that, including a test that builds a
**live-mode** `ChatClient` with a key that passes the placeholder check and
asserts the call dies on the wall rather than reaching the provider.

Before this existed the guarantee was convention only: tests stayed offline
because each one passed `demo_mode=True` or injected a fake client, so a single
new test in the wrong shape could have spent real credits in CI.

| File | Covers |
|---|---|
| `test_adjudicate.py` | The adjudicator may add an opinion. It may not add a decision |
| `test_api.py` | real HTTP against the FastAPI app, ZIP contents, traversal |
| `test_api_auth.py` | the API-key gate, the session cookie, and a scan proving no data route ships unprotected |
| `test_client.py` | payload shape, probe, ladder call counts, backoff, cache |
| `test_compare.py` | tolerance bands, cardinality gate, arbitration, outliers |
| `test_config.py` | runtime Configure: masking, client rebuild, 400/403 refusals |
| `test_config_token.py` | The optional shared secret on the config endpoints (docs/AUDIT.md R1) |
| `test_consensus.py` | Second-reader consensus: a review marker, never a tie-break |
| `test_correction_email.py` | Correction drafts: written by rule, and never by a model |
| `test_corrections.py` | Human corrections: verdicts move instantly, the model's answer survives |
| `test_discovery.py` | Model discovery, tested against catalogs rather than live hosts |
| `test_docs_freshness.py` | the docs' own claims: cache counts derived from `fixtures/`, the field count from `app/fields.py`, screenshots present and referenced, every relative link resolving |
| `test_eval.py` | The evaluation harness, tested |
| `test_evidence_pipeline.py` | An invented field must not be allowed to invent a disagreement |
| `test_fixture_truth.py` | The fixture data has to be true to the page it claims to show |
| `test_hardening.py` | Tests for the Phase 4 hardening: what an upload is allowed to be, what it is |
| `test_lineitems.py` | Goods-line matching: opt-in, exact, and incapable of moving a verdict |
| `test_live_fixtures.py` | captured **live** responses; transcription and confidence quirks pinned |
| `test_mathcheck.py` | Arithmetic inside one document: pass, fail, and within tolerance |
| `test_narrative_cache.py` | The executive-summary cache: verdict-keyed, disk-backed, tolerant of junk |
| `test_normalize_categorical.py` | Incoterms, PO references and origin: what must unify, what must never be guessed |
| `test_normalizers.py` | number/weight/quantity/container/HS/party/money tables |
| `test_offline_guard.py` | the socket guard itself: remote refused, loopback allowed |
| `test_parse.py` | retry-ladder rungs 1–3, "one bad field ≠ dead document" |
| `test_pdf.py` | the PDF's text layer, its provenance wording, and markup injected from a document |
| `test_pipeline.py` | all three fixtures end to end through DEMO_MODE |
| `test_planted_bundle.py` | The planted-error bundle: its shape, its score, and its one declared miss |
| `test_preprocess.py` | Optional ingest preprocessing: auto-orient, deskew, contrast |
| `test_progress.py` | Progress fan-out: the store hook, the queues, and the SSE route |
| `test_prompt.py` | every contracted field is asked for, on every rung of the ladder |
| `test_providers.py` | two-provider routing, cache isolation, reasoning fields, summary safety |
| `test_rules.py` | Cross-field sanity rules |
| `test_scoring.py` | The 0-100 score, and the rule behind every point in it |
| `test_severity.py` | Severity, and the one property the banner has to keep |
| `test_spend.py` | The spend log: exact tokens, estimated dollars, budget guard |
| `test_store.py` | The filesystem run store: safe names, atomic writes, tolerant reads |
| `test_textlayer.py` | Recovering what a page says without asking the model that read it |
| `test_transparency.py` | Evidence the run keeps: raw bodies, the host that answered, and token cost |
| `test_verify.py` | Does the quoted line exist on the page, and does the value? |

Zero API calls in the whole suite. `test_client.py` and `test_providers.py` use
fake transports, so they pin the exact `response_format` payload each provider
receives without a key.

---


## Verification status

**Both providers have been called live and both work.** What is verified, and
what is not:

The live rows below rest on the captured responses and raw bodies committed
under `fixtures/live_responses/`, which is why they can be checked without a
key. They were **not re-run during the October 2026 audit**, because no API key
was available; nothing on this list has been re-verified against a live endpoint
since. Model availability on both hosts changes without notice —
`nvidia/nemotron-nano-12b-v2-vl` is on this list precisely because it went 410
Gone mid-project — so treat every ✅ as "true as of the capture, re-run
`make probe-live` to confirm".

| | Status |
|---|---|
| Normalizers, comparators, parse ladder | ✅ the offline suite is green, and the browser suite runs against a real server |
| The status banner never contradicts the verdict or the risk strip | ✅ pinned from both directions, and proved to fail: flipping the container row from critical to warning breaks 8 tests, flipping the country-of-origin row back to warning breaks 2, and removing the verdict floor breaks 3 |
| Correction drafts name no value no document printed | ✅ asserted over every draft, and the ZIP manifest bug that hid README.txt's header is pinned |
| The engine against known ground truth | ✅ 9/9 planted errors caught, 0/9 false alarms on clean and tricky paperwork, 1 miss declared in advance — `docs/PLANTED.md`, 0 API calls |
| The evidence highlight lands on the right words | ✅ screenshot-verified in chromium (the rectangle and the image share one box), and proved to fail: dropping the verbatim-line restriction breaks the fuzzy test, and stopping the chip from passing its box breaks the browser suite |
| `runs/{id}/report.json` is the same bytes on Windows and in the container | ✅ was not — pydantic writes unescaped non-ASCII, and `write_text()` used the locale codec, so an em dash landed as `0x97`. Encoding is now pinned to utf-8 on write and read |
| End-to-end pipeline, UI, ZIP export, HTTP API | ✅ cache-verified **and** live-verified |
| Vision extraction, NVIDIA build.nvidia.com | ✅ 12/12 fixture documents captured live (omni, 2026-10-16 re-capture); every one parsed on ladder rung 1 |
| Vision extraction, Nebius MiniCPM-V (fallback) | ✅ 12/12 documents, all parsed on attempt 1 |
| Vision extraction, Nebius Super-VL (selectable) | ✅ 12/12 documents captured live on 2026-10-23, and 16/16 across the fixture + stress pages on 2026-10-21 |
| Nebius summary call (the hackathon requirement) | ✅ live, one call per shipment |
| `response_format: json_schema` (strict) | ✅ accepted on **both** hosts — wrapped shape only |
| Base64 data URIs | ✅ accepted on both |
| Checksum arbitration on live output | ✅ flagged the planted bad container number |
| Cross-page evidence (`page_index`) | ✅ live model reported page 2 correctly |
| Extraction determinism | ⚠️ **not guaranteed.** Two calls on one page were byte-identical, but a 6-call re-capture on 2026-10-10 moved NVIDIA's `incoterms` *value* on 2 of 3 invoices and shortened 2 evidence *quotes*. Nebius was byte-stable across the same re-capture. `compare_key` exactness (75/75) and every shipment verdict were unchanged. |
| Pricing / context / rate limits | ✅ live `GET /v1/models?verbose=true` (Nebius) |
| `nvidia/nemotron-nano-12b-v2-vl` (brief's model) | ❌ **410 Gone on build.nvidia.com**, 404 on Nebius |
| `docker build` | ✅ built, smoke-tested and `HEALTHCHECK`-checked in GitHub Actions (run 39915598051, 2026-10-23) — **never on this machine**, which has no Docker daemon |

---
