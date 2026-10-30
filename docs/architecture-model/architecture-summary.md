# How to read the architecture diagram

`architecture.png` is one view: the **runtime structure of a single instance**, top
to bottom, from the person in the browser to the two provider APIs. It is drawn
from the code at commit `0be84c1` — the subtitle in the picture states that commit,
so a diagram older than the tree is visible rather than silently trusted.

Line styles carry meaning: **solid** = a runtime call, **dashed** = data access
(read or write of a file), **dotted** = a step that is optional or off by default.

## Reading order

1. **Browser.** The reviewer holds no state. The UI shells are server-served HTML
   plus vanilla JS; the "transport" box is separate because the interesting
   decision is that progress is a polled JSON file plus an optional SSE stream, not
   a session held open in the tab.
2. **HTTP API.** One FastAPI process. Every request enters the same box; the wide
   routes node lists all 22, and the five boxes under it are the guards each
   data-bearing request passes — key or session, per-client throttle, daily cap,
   the spend ledger they are measured against, and the token-gated config view.
   Two endpoints are deliberately outside the key: `/api/health` (for probes) and
   `/api/session` (the key exchange itself).
3. **Reconciliation pipeline.** One asyncio task per shipment, dispatched by the
   route and finished after the response. Seven stages, in order. The dotted row
   beneath is real work that is *not* on the critical path: consensus, line items
   and the adjudication pass can add calls but cannot change a verdict, which is
   computed by the deterministic stages above them.
4. **Model access.** Three clients, all speaking HTTPS to the two boxes on the
   right. Extraction is primary-to-fallback across providers; discovery reads the
   catalog so a model name is not a guess.
5. **Filesystem.** There is no database. `runs/<id>/` is the record of a shipment,
   `fixtures/responses/` is committed provenance read first, and `.live_cache/` is
   where a machine's own captures are written. The split is the point: a live run
   never writes into reviewed fixtures.
6. **Delivery.** The whole thing is one container, configured two ways.

## What is worth noticing

- **The report is the interface.** A run's state lives in `report.json` on disk,
  which is why the UI can poll, why SSE is an accelerator rather than a
  requirement, and why a disconnected tab costs nothing.
- **Guards sit before dispatch, not inside the pipeline.** The cap and the
  reservation are decided at the door, so a refusal never starts a run that would
  have to be killed later.
- **Model calls are bounded by deterministic code.** Extraction transcribes; the
  comparison, the arithmetic checks, the score and the status are Python. The
  optional model opinions are drawn below the line for that reason.
- **Two caches, two rules.** Reads check the committed tree first; writes go
  elsewhere. That asymmetry is deliberate and is what keeps the demo's
  provenance claims checkable.

## Regenerating

```
.venv/Scripts/python.exe scripts/render_architecture.py
```

The structure — every node, coordinate and edge — is in that script, not in the
SVG, so editing the picture means editing one table. It writes `architecture.svg`
and rasterises it at 2× with the Chromium build the e2e suite already uses.

Graphviz is **not** installed here and there is no network to add it, so the
delivered source is SVG rather than DOT. `architecture-evidence.md` records why,
and its node/edge tables transpose to DOT directly if you later want that form.

## What this view deliberately does not answer

- **Where it runs and how it scales** — deployment topology, not structure.
- **What a shipment costs in tokens or dollars** — configuration and
  `docs/PROVIDERS.md`.
- **The ten-field comparison rules** — `docs/ARCHITECTURE.md`.
- **Business behaviour** (who uploads, what a reviewer decides) — this is a
  technical structure view, not a capability or journey map.
