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
| Keys never reach logs, reports, ZIPs, PDFs or error bodies | `/api/config` returns a mask; 401 bodies carry no key material; the export ZIP is built from the report model, which has no key field | A future handler that logs `request.headers` wholesale would leak the `X-API-Key`. Not covered by a test today |
| A browser cannot send headers everywhere, so it gets a session | `POST /api/session` exchanges a key for an `HttpOnly; SameSite=Strict` cookie, used by `<img>`, `EventSource` and downloads | The cookie is a bearer credential for the run's origin; CSRF is mitigated by `SameSite=Strict`, not by a token |
| Untrusted text is data, never instructions | Prompt rule 5; the UI builds every node with `textContent`; the PDF escapes; unknown JSON keys are dropped before validation | A model that ignores the instruction still cannot execute anything, because no path evaluates document text |
| The one endpoint that spends money is throttled | `app/limits.py` sliding window, `RUN_LIMIT_PER_MINUTE` per client | Reading reports is unlimited by design — the UI polls every second. Behind a proxy, let the proxy do it properly |
| Uploads are bounded before they are believed | `_read_capped` streams with a byte ceiling; magic bytes decide the type, not the filename; an explicit pixel ceiling plus Pillow's bomb limit | A PDF that expands to 6 large pages is still ~6 decodes per document; `MAX_PAGES_PER_DOCUMENT` bounds it |
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
   under the run directory only.
2. **HTML/JS injection from document text** — values reach the DOM through
   `textContent` and the PDF through `html.escape`. Pinned by parametrized tests in
   `tests/test_pdf.py` and `tests/test_lineitems.py`.
3. **Prompt injection** — document text instructing the model. Mitigated by prompt
   rule 5 and, more importantly, by the architecture: even a fully compliant
   injected instruction can only change which *strings* get transcribed, and every
   verdict is computed from those strings by Python that never asks the model.
4. **Zip/path abuse on export** — archive members are named from the shipment id
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
