# Architecture decision records

One decision per file, short, with the evidence that settled it. These are written
after the fact in most cases — the codebase was audited before this directory
existed — but each one records what was measured, not what was fashionable.

| # | Title | Status |
|---|---|---|
| [0001](0001-model-transcribes-python-decides.md) | The model transcribes; deterministic Python decides | accepted |
| [0002](0002-canonical-and-compare-key.md) | `canonical` and `compare_key` are separate values | accepted |
| [0003](0003-no-currency-conversion.md) | Never convert currencies | accepted |
| [0004](0004-filesystem-as-database.md) | Filesystem as the run store; no database | accepted |
| [0005](0005-cache-keyed-on-the-contract.md) | The cache key includes the contract version | accepted |
| [0006](0006-line-items-match-exactly.md) | Goods-line matching is exact, never fuzzy | accepted |
| [0007](0007-offline-guard-self-bound-only.md) | The offline guard permits only self-bound loopback ports | accepted |
| [0008](0008-frontend-stays-one-file.md) | The frontend stays one script; browser tests are added instead | accepted |
| [0009](0009-optional-model-calls-are-additive.md) | Advisory model calls can never change a verdict | accepted |
