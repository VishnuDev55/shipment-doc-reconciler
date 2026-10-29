# Security model

What this system protects, from whom, and what it explicitly does not protect
against. Written so that a reviewer can disagree with a specific line rather than
with a mood.

## What is being protected

Other people's commercial data. A shipment report contains a supplier's prices, a
buyer's identity, an HS code that may be wrong, and a container number. Uploaded
documents are images of the same. The threat model starts from "this is not my
data" — which is why the API can be closed and the demo default cannot be shipped
by accident.

## The seven rules, and what each one costs

| Rule | Implementation | Residual risk |
|---|---|---|
| Keys are never stored, only compared | `hmac.compare_digest` against `API_KEYS`; the Configure drawer writes keys into the running process only when `GUI_CONFIG_ENABLED=true` | A process with a key in memory can be dumped by anyone who can already read process memory |
| Keys never reach logs, reports, ZIPs, PDFs or error bodies | `/api/config` returns a mask (`_mask`, `app/config_view.py:21`); 401 bodies carry no key material; the export ZIP is built from the report model, which has no key field. The mask route is behind `require_api_key`, and on the deployed instance behind `CONFIG_TOKEN` as well now that `render.yaml` carries it as a dashboard secret rather than leaving it blank | The mask still discards a key's first five and last four characters to whoever can read it — enough to recognize a key you already have. `GUI_CONFIG_ENABLED=false` closes the write path; the token is what closes the read. A future handler that logs `request.headers` wholesale would leak the `X-API-Key`, and that is still not covered by a test |
| A browser cannot send headers everywhere, so it gets a session | `POST /api/session` exchanges a key for an `HttpOnly; SameSite=Strict` cookie, used by `<img>`, `EventSource` and downloads | The cookie is a bearer credential for the run's origin; CSRF is mitigated by `SameSite=Strict`, not by a token |
| Untrusted text is data, never instructions | Prompt rule 5; the UI builds every node with `textContent`; the PDF escapes; unknown JSON keys are dropped before validation | A model that ignores the instruction still cannot execute anything, because no path evaluates document text |
| The endpoints that spend money are throttled, capped, and reserved | `_gate_spending()` (`app/main.py:448`) runs the daily cap check and the `app/limits.py` sliding window for **both** paths to a billed call: creating a shipment and retrying one document. A retry used to reach the vision model through a door nobody gated, which made the cap a ceiling with a hole in it. `RUN_LIMIT_PER_MINUTE` is **per client**, identified by API key if one is offered and by peer address otherwise | Reading reports is unlimited by design — the UI polls every second. Behind a proxy, set `TRUSTED_PROXY` so the limiter uses the forwarded hop; without it the header is ignored, because a caller who can choose their own identity has no limit at all |
| Uploads are bounded before they are believed | `_read_capped` streams with a byte ceiling; magic bytes decide the type, not the filename; an explicit pixel ceiling plus Pillow's bomb limit; and a PDF is now sized from the page dimensions it **declares**, before it is drawn | A live run still costs one decode per page; `MAX_PAGES_PER_DOCUMENT` bounds that. The declared-size clamp is what stops a sub-MiB PDF asking for gigabytes |
| The test suite cannot spend money | `tests/conftest.py` patches `connect`, `connect_ex`, `create_connection`, `getaddrinfo`, `gethostbyname` for the session | See the loopback story below |

## HTTP surface

Sent on every response (`app/main.py:security_headers`):

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self';
  font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'
```

`style-src 'unsafe-inline'` is the one loosened directive, and it is the feature
rather than an oversight: the exported standalone HTML report embeds its own CSS so
it opens correctly in an email client or a file viewer. Scripts stay same-origin and
there is no CDN anywhere in the app, so that costs nothing on the script side.
`img-src data:` is required because pages are sent to the model as data URIs.

CORS is **off by default**. Set `CORS_ORIGINS` to let another site's JavaScript
ride a user's session — which is a product decision, not a security boundary.

## Authentication contract

`X-API-Key: <key>` or `Authorization: Bearer <key>` on every data-bearing route.
`/api/health` stays open so a monitor can distinguish an outage from an auth wall.
When `API_KEYS` is unset the API is open, and `/api/health` reports
`api_protected: false` so nothing pretends to be closed.

On a deployment that does set `API_KEYS`, health answers a caller without a key or
session with `status`, `mode`, `demo_mode` and `api_protected` and nothing else: the
model ids, hostnames, version, commit and retention window are a description of
what to target, and a liveness probe does not need them. The UI re-polls once a
session is open, which is when those names become useful to the person reading them.

Comparisons are constant-time on UTF-8 bytes (`secret_matches`, `app/auth.py`), not
on `str`: `hmac.compare_digest` raises `TypeError` on non-ASCII input, and a header
is decoded from arbitrary bytes, so a well-typed crash there used to answer a
request with a 500 before deciding whether to trust it.

Verified in `tests_e2e/test_ui.py`: a protected instance refuses `/api/shipments`
with 401 and echoes nothing about the key; the browser unlock flow works with a
correct key and stays open after a wrong one; and after unlocking, the key appears
in neither `localStorage` nor `sessionStorage`.

## The offline guarantee, and the hole that made it false

`tests/` has always been offline. For most of the project's life that was a
convention, not a mechanism. The guard now patches the socket layer, and it has
one interesting property worth understanding:

A local proxy listening on `127.0.0.1` can forward to the internet. The first
version of the guard allowed loopback ("so a test can bind an ephemeral port"), and
a real HTTPS request to a provider completed *inside the guarded suite*. Refusing
loopback outright broke every API test, because Starlette's `TestClient` starts an
anyio blocking portal that **is** a loopback socket pair on Windows.

The distinction that matters is not the address but who opened it. `conftest.py`
now records every port the process binds via a wrapped `bind()` and permits a
connection only to one of those. An external listener on loopback is refused on the
same grounds as a remote host. Resolving a loopback *name* is still allowed: it
puts nothing on a wire, and asyncio does it for its own plumbing.

The browser suite in `tests_e2e/` is outside this guarantee by construction — it
opens real sockets — so it lives in a separate directory that `testpaths` cannot
collect, and it skips loudly if the guard is ever active in its session.

## Injection surface, ranked by how likely it is to matter

1. **Path traversal on evidence images** — `/page/{shipment_id}/{path}` resolves
   under the run directory, and cannot be made to resolve anywhere else. The id is
   validated at the single place a caller-supplied id meets the filesystem
   (`RunStore.dir`, `app/store.py:111`), so all 11 callers inherit it; the route
   additionally requires the run to exist and re-checks containment against the runs
   root. Before that, a `shipment_id` of `..` moved the base directory itself up one
   level and the containment test then passed for `.env`. Pinned by
   `test_page_route_blocks_traversal_through_the_id_segment` — which, unlike the
   test that preceded it, creates the file it asks for, so a 404 means a refusal
   rather than a missing path — and by `test_store.py`'s parametrized id shapes.
2. **HTML/JS injection from document text** — values reach the DOM through
   `textContent`, the PDF through `html.escape`, and the downloadable standalone
   report through `esc()` on every interpolation. Pinned by parametrized tests in
   `tests/test_pdf.py:217-229,297-306` and, for the standalone report, by
   `test_document_text_cannot_mark_up_the_standalone_report`. That second test is
   new: this file used to cite `tests/test_lineitems.py` for it, and no such
   assertion existed anywhere. The standalone report also carries a meta
   `Content-Security-Policy` (`default-src 'none'`) now, because it leaves this app
   as a file and inherits no header — escaping is the control, the policy is the
   second lock.
3. **Spreadsheet formulas in the CSV export** — a document value beginning `=`,
   `+`, `-`, `@`, tab or CR is written with a leading apostrophe
   (`_csv_cell`, `app/export.py`), so opening `report.csv` cannot execute a
   document's text. Pinned by six payloads plus a negative amount that must stay a
   number.
4. **Prompt injection** — document text instructing the model. Mitigated by prompt
   rule 5 and, more importantly, by the architecture: even a fully compliant
   injected instruction can only change which *strings* get transcribed, and every
   verdict is computed from those strings by Python that never asks the model.
5. **Zip/path abuse on export** — archive members are named from the shipment id
   and fixed labels, not from filenames.

## What is not protected

- **No accounts, no roles.** `actor` on a correction is free text, not identity.
  The audit trail records who someone said they were, which is still worth more
  than nothing and much less than authentication.
- **Single-tenant by design.** One process, one `runs/` directory, no isolation
  between shipments. Multi-tenant deployment needs per-tenant run directories and
  an authorization layer that does not exist here.
- **At rest.** `runs/` is plaintext on disk. Encryption is a filesystem or
  platform concern in this design, and `RUN_TTL_HOURS` is the retention control.
- **Transport.** TLS is terminated by the platform (Render, or your reverse proxy).
  The app does not enforce HSTS because it cannot see the scheme reliably.

## Deploying this honestly

The default configuration is open: `API_KEYS` unset, `GUI_CONFIG_ENABLED=true`,
`RUN_TTL_HOURS=0`. That is correct on a laptop and wrong on a host. The minimum for
anything reachable by others is `API_KEYS`, `GUI_CONFIG_ENABLED=false`,
`RUN_TTL_HOURS`, and TLS in front. `docs/DEPLOYMENT.md` has the commands.

That minimum is now partly enforced rather than advised. An instance that starts
with `DEMO_MODE=false` and an empty `API_KEYS` **refuses to boot** (`_lifespan`,
`app/main.py:90`) and says which of the three ways out it thinks you want. The rest
is still your decision, which is why `docker-compose.yml` ships live with the key
passed through from your `.env` rather than inventing a default one.

The reason a boot check rather than a warning: `require_api_key` returns
immediately when `API_KEYS` is empty. That is what makes the keyless hosted demo
work, and it is the same line that would have left every data-bearing route —
including the one that reads files from disk — open on a live deployment. The
failure is invisible from the outside: `/api/health` answers 200 either way.
