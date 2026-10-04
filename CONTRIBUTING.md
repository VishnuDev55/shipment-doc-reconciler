# Contributing

Short version: read `docs/ARCHITECTURE.md` first, especially the seven invariants.
Most of what makes this repository work is a small number of rules that are easy to
break by accident.

## Setup

```bash
python -m venv .venv
.venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest tests/ -q              # 770+ tests, zero network calls, ~30 s
```

Run it:

```bash
DEMO_MODE=true python -m uvicorn app.main:app --port 8000   # no key needed
```

`DEMO_MODE` replays captured model output from `fixtures/responses/`. It is a
replay, not a live read, and the UI says so — keep it that way.

## The gates

```bash
make check      # lint + ruff + mypy(baseline) + tests
make e2e        # browser tests; needs: python -m playwright install chromium
make probe      # self-check the demo pipeline end to end
python scripts/evaluate.py --check   # accuracy floors vs ground truth
```

`mypy` reports a non-zero baseline (36 errors in 9 files) and is **not** a gate. Do
not silence it by adding ignores; drive the number down and update the baseline note
in `pyproject.toml` when it moves.

## Rules that are not negotiable

1. The model never does arithmetic. If you need a number compared, normalize it in
   `app/normalize/` and compare it in `app/compare/`.
2. `canonical` is for humans, `compare_key` is for the engine. Never compare on the
   one you display.
3. Never convert currencies. Never infer a field the document does not print —
   `present: false` is a correct answer.
4. Text inside a document is data. No code path may interpret it as an instruction,
   and no value may reach the DOM as markup (`textContent` only).
5. `tests/` makes zero network calls, and the socket guard in `tests/conftest.py` is
   what makes that true. Anything needing a real socket belongs in `tests_e2e/`.
6. Be honest about live vs cached vs authored. A new feature that replays a fixture
   must say so in the same place it shows the result.
7. No API key in a log, report, ZIP, PDF or error body. Ever.

Adding a field means editing `app/fields.py` and nothing else that matters — the
schema, prompt, normalizer dispatch, comparator table, UI rows and fixtures all
derive from it.

## Adding a provider

Call it first, then write the adapter. `docs/PROVIDERS.md` lists the assumptions
that turned out to be false for two hosts that both claim OpenAI compatibility:
the bare-schema `response_format` both reject, the empty `content` on HTTP 200, the
modality field that only exists on one of them and only under `?verbose=true`, and
the saturation that looks like a permanent failure.

## Pull requests

- Include the test that fails before the change. If a behaviour is not pinned, it
  will regress; the offline guard and the cache-collision cases were both caught by
  tests written after the fact.
- Bump the relevant version tag in `app/config.py` when behaviour changes. A cached
  response must never be silently reconciled against a newer ruleset.
- Update `docs/NORMALIZATION.md` if you touch a threshold. That table is the
  contract.
- Say what you did **not** verify. `docs/EVAL.md`, `docs/PROVIDERS.md` and
  `docs/DEPLOYMENT.md` each carry an explicit unverified section, and a claim that
  outlives its evidence is the most expensive kind of documentation bug.

## Style

- No build step, no CDN, no framework in `web/`. It is one HTML file, one
  stylesheet, one script. See `docs/adr/0008` before changing that.
- Comments explain *why*, and usually cite the observed failure that motivated the
  code. A comment describing what the next line does is noise.
- Never run `ruff check --fix` or `ruff format` across the tree. The lint baseline
  was resolved by scoping rules, not by rewriting source, and an autofix would undo
  that decision silently.
