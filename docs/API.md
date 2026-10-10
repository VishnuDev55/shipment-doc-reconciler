# HTTP API

Every feature the browser UI has is an HTTP call. This is the reference for
using the app as a service rather than a page: the same routes, the same JSON,
no UI-specific behaviour anywhere.

Interactive docs are served by the app itself at **`/docs`** (OpenAPI at
`/openapi.json`), including the Authorize button described below.

---

## Authentication

The app is open by default, which is right on a laptop and wrong on a shared
host. Set `API_KEYS` to turn it into a keyed service:

```bash
API_KEYS="k-live-2f7c…,k-partner-9be1…"   # comma-separated; any one is accepted
```

Then every data-bearing route requires a key:

```
X-API-Key: k-live-2f7c…
Authorization: Bearer k-live-2f7c…        # equivalent
```

| | |
|---|---|
| What is gated | `/api/*` except `/api/health` and `/api/session`, plus `/page/*` (evidence images) |
| What stays open | `/` and `/static/*` (the UI shell), `/docs`, `/openapi.json`, `/api/health` |
| Why health is open | A load balancer has to answer "is it up?" without holding a secret |
| Why no `?api_key=` | A URL is written to access logs, browser history and `Referer` headers. A key that travels there has already leaked, so query-string auth is not supported even though it would have made download links simpler |
| Comparison | Constant-time against each configured key |
| Leakage | A rejection never echoes the supplied key or the expected one |

### Browsers and the session cookie

A browser cannot put a header on an `<img>`, an `EventSource` or a download
link, so header-only auth would quietly break live progress, evidence images and
exports in a protected deployment. `POST /api/session` fixes that: it checks the
key, then returns a short random nonce as an `HttpOnly; SameSite=Strict` cookie.
The key itself is never stored, returned, or written to the report.

```bash
curl -si -X POST http://localhost:8000/api/session \
     -H 'content-type: application/json' -d '{"key":"k-live-2f7c…'}
# Set-Cookie: sdr_session=<nonce>; HttpOnly; SameSite=strict; Max-Age=43200
```

Sessions live in process memory and expire after 12 hours, so restarting the
service invalidates them. That is deliberate: a cookie that survived a restart
would be a credential with no way to revoke it.

`GET /api/health` reports `"api_protected": true|false`, which is how the UI
knows to ask for a key before it asks for anything else.

### Headers and cross-origin calls

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer` and a same-origin `Content-Security-Policy`. The
policy allows inline styles on purpose: the exported report embeds its own CSS so
it opens anywhere, which is the feature rather than an oversight.

Cross-origin calls are refused by default — no `CORS_ORIGINS`, no
`Access-Control-Allow-Origin`. Listing origins there lets a specific site's
JavaScript call the API with a user's session cookie attached. That is a
deployment decision made at startup, not a per-request one.

---

## The shape of a run

```
POST /api/shipments            → {"shipment_id": "…"}
GET  /api/shipments/{id}/report   until "state": "done"
     (or GET /api/shipments/{id}/events for server-sent updates)
GET  /api/shipments/{id}/export   → ZIP: json + csv + html + pdf + pages + raw
```

A shipment is 1–5 documents (`.pdf`, `.png`, `.jpg`, `.webp`), each at most
25 MB. Both limits are configurable (`MAX_DOCUMENTS`, `MAX_UPLOAD_BYTES`) and
both are enforced before anything is read into memory.

`report.json` is the whole model: `run` (which provider, which model, which
prompt/normalizer/comparator versions, how many live calls this run made),
`documents[]` (per-document source, usage, latency, attempts, page refs, and
`verification` -- a state per field saying whether its quoted line was found on
the page), `comparisons[]` (one per field: status, reason, and every document's
verbatim value beside its canonical form), `unverified[]` (fields withdrawn from
the comparison because their evidence was not printed), `internal_checks[]`
(arithmetic inside each document, with `not_run` kept distinct from `pass`),
`line_items` (the opt-in goods-line match, or a statement that it was not asked
for), `sanity[]`, `risk`, `status_report` (one severity per finding and the
banner that follows from it), `correction_emails[]` (the drafts that banner
implies, written by rule with no model call), `summary`, `narrative`, `briefing`.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Status, both providers' readiness, model discovery results, `api_protected`. Always open |
| `POST` | `/api/session` | Exchange an API key for the browser session cookie |
| `DELETE` | `/api/session` | Revoke the current session |
| `GET` | `/api/fixtures` | The three bundled demo shipments |
| `GET` | `/api/fixture/{name}` | Run one demo shipment from the bundled cache; returns an id to poll |
| `POST` | `/api/shipments` | Upload documents. Multipart: `files` (repeat), optional `doc_types` as a JSON array of strings or nulls, aligned by position |
| `GET` | `/api/shipments` | Ids of runs held on disk |
| `GET` | `/api/shipments/{id}/report` | The full report (see above) |
| `GET` | `/api/shipments/{id}/events` | Server-sent events: one frame per report write, so progress is pushed rather than polled |
| `POST` | `/api/shipments/{id}/retry` | Re-read one failed document. Body `{"document_id": "…"}` |
| `POST` | `/api/shipments/{id}/documents/{docId}/fields/{field}` | Correct one value. Body `{"value": "…", "actor": "…", "note": "…"}` |
| `POST` | `/api/shipments/{id}/documents/{docId}/fields/{field}/revert` | Undo the active correction |
| `GET` | `/api/shipments/{id}/corrections` | The audit trail, active and reverted |
| `GET` | `/api/shipments/{id}/export` | ZIP of everything above, including evidence pages and raw model bodies |
| `GET` | `/api/shipments/{id}/report.pdf` | The same report laid out for print and filing |
| `GET` | `/api/shipments/{id}/raw/{name}` | A raw model response the report listed by name |
| `GET` | `/api/models` | What model discovery decided for each role, and how |
| `POST` | `/api/models/refresh` | Re-query both hosts now, bypassing the cache |
| `GET` | `/api/config` | Current configuration, keys masked |
| `POST` | `/api/config` | Apply configuration at runtime. Needs `X-Config-Token` when `CONFIG_TOKEN` is set, and is refused entirely when `GUI_CONFIG_ENABLED=false` |
| `GET` | `/page/{id}/{path}` | A normalized page image, for evidence |

Field keys are exactly `quantity`, `gross_weight`, `net_weight`,
`container_number`, `hs_code`, `consignee_name`, `invoice_value`, `incoterms`,
`po_number`, `country_of_origin`. Comparison statuses are `consistent`,
`suspicious`, `conflict`, `single_source`, `absent`.

---

## Clients

### curl

```bash
BASE=http://localhost:8000
KEY='k-live-2f7c…'

sid=$(curl -s -X POST $BASE/api/shipments \
        -H "X-API-Key: $KEY" \
        -F 'files=@invoice.png' -F 'files=@packing_list.png' \
        -F 'doc_types=["commercial_invoice","packing_list"]' \
      | python -c 'import json,sys; print(json.load(sys.stdin)["shipment_id"])')

curl -s -H "X-API-Key: $KEY" $BASE/api/shipments/$sid/report \
  | python -c 'import json,sys; r=json.load(sys.stdin); \
print(r["state"], r["summary"]["verdict"]); \
[print(f"  {c[\"field\"]:18} {c[\"status\"]}") for c in r["comparisons"]]'

curl -s -OJ -H "X-API-Key: $KEY" $BASE/api/shipments/$sid/report.pdf
```

### Python

```python
import json
import time
from pathlib import Path

import httpx


class ReconcilerClient:
    """A thin client for the reconciliation API.

    Polls rather than streams: SSE is available at /events and is what the
    browser uses, but a batch job usually wants one request and an answer.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 180.0):
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, headers={"X-API-Key": api_key}
        )

    def reconcile(self, *paths, doc_types=None, poll_seconds=1.0):
        files = [("files", (Path(p).name, Path(p).read_bytes(), _content_type(p)))
                 for p in paths]
        data = {}
        if doc_types:
            data["doc_types"] = json.dumps(list(doc_types))

        created = self._http.post("/api/shipments", files=files, data=data)
        created.raise_for_status()
        shipment_id = created.json()["shipment_id"]

        deadline = time.monotonic() + self._http.timeout.read
        while time.monotonic() < deadline:
            report = self._http.get(f"/api/shipments/{shipment_id}/report")
            report.raise_for_status()
            body = report.json()
            if body["state"] in ("done", "partial", "failed"):
                return body
            time.sleep(poll_seconds)
        raise TimeoutError(f"shipment {shipment_id} did not finish in time")

    def pdf(self, shipment_id: str) -> bytes:
        res = self._http.get(f"/api/shipments/{shipment_id}/report.pdf")
        res.raise_for_status()
        return res.content


def _content_type(path) -> str:
    return "application/pdf" if str(path).lower().endswith(".pdf") else "image/png"


client = ReconcilerClient("http://localhost:8000", "k-live-2f7c…")
report = client.reconcile("invoice.png", "bol.png",
                          doc_types=["commercial_invoice", "bill_of_lading"])

for comparison in report["comparisons"]:
    if comparison["status"] in ("conflict", "suspicious"):
        print(comparison["field"], comparison["status"], comparison["reason"])

print("risk:", report["risk"]["level"], report["risk"]["score"])
client._http.close()
```

### JavaScript

```js
const base = "http://localhost:8000";
const headers = { "X-API-Key": import.meta.env?.VITE_SDR_KEY };

const form = new FormData();
form.append("files", invoiceFile, "invoice.png");
form.append("files", packingListFile, "packing_list.png");
form.append("doc_types", JSON.stringify(["commercial_invoice", "packing_list"]));

const { shipment_id } = await (await fetch(`${base}/api/shipments`,
  { method: "POST", headers, body: form })).json();

let report;
do {
  await new Promise(r => setTimeout(r, 1000));
  report = await (await fetch(`${base}/api/shipments/${shipment_id}/report`,
    { headers })).json();
} while (report.state === "running" || report.state === "pending");
```

---

## Errors

Failures are `{"detail": "…"}` with an appropriate status:

| Status | When |
|---|---|
| `400` | No files, more than `MAX_DOCUMENTS`, an unsupported suffix, a malformed `doc_types` |
| `401` | Missing or invalid API key; missing config token |
| `404` | Unknown shipment id, or a raw body the report never listed |
| `413` | A document over `MAX_UPLOAD_BYTES` |
| `500` | PDF or ZIP rendering failed — the report itself is unaffected and still readable at `/report` |

A document that fails to extract does **not** fail the shipment: it is reported
with `status: "failed"` and its error, coverage is stated as incomplete, and the
risk score takes a penalty for it. Silent partial results are the failure mode
this shape exists to prevent.

---

## What this API deliberately does not do

- **No accounts, no per-user quota.** One shared process, one shared key set.
  `actor` on a correction is free text, recorded because an unattributed change
  is harder to defend to an auditor — not because it authenticates anyone.
- **Rate limiting covers the endpoint that costs money, and nothing else.**
  `RUN_LIMIT_PER_MINUTE` (default 30) bounds `POST /api/shipments` per API key,
  falling back to the peer address; over that you get `429` with `Retry-After`.
  Reading reports is deliberately unlimited, because the browser polls it every
  second and throttling that would break the UI to protect an endpoint that costs
  nothing. It is a per-process sliding window: behind a reverse proxy, set it to
  `0` and limit there instead.
- **No webhooks.** Poll the report, or hold the SSE stream.
- **No persistence guarantee across restarts.** Runs live under `RUNS_DIR`
  (default `./runs`) as plain files; mount a volume if they must survive.
- **No arithmetic, no currency conversion, no inferred fields.** The model
  transcribes; deterministic Python compares. A field no document states comes
  back `absent`, never a guess.
