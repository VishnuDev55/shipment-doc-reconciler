PY := python3

.PHONY: help setup setup-runtime fixtures dev run test lint ruff type coverage check probe probe-live probe-diff capture-summaries migrate dist clean docker compose

help:
	@echo "make setup       install runtime + dev tooling (what a contributor wants)"
	@echo "make setup-runtime  install only what the Docker image installs"
	@echo "make fixtures  (re)generate synthetic shipments + DEMO_MODE response cache"
	@echo "make dev       run the app in DEMO_MODE with autoreload"
	@echo "make run       run the app (reads .env; set DEMO_MODE=false for live)"
	@echo "make test      run the test suite (zero API calls)"
	@echo "make lint      pyflakes over app/ scripts/ tests/"
	@echo "make ruff      ruff over app/ scripts/ tests/ fixtures/ (never --fix)"
	@echo "make type      mypy app -- prints the baseline, does not gate"
	@echo "make coverage  pytest under coverage; floor 88 vs measured 90"
	@echo "make check     the CI gate: lint + ruff + type + test"
	@echo "make probe     self-check the demo pipeline end to end"
	@echo "make probe-live     verify BOTH providers live (vision + Nebius summary)"
	@echo "make probe-diff     live extractions diffed vs the authored cache"
	@echo "make capture-summs  capture real AI summaries for the demo cache"
	@echo "make migrate        re-key cache entries after a cache-format change"
	@echo "make dist      write a clean source zip to dist/"
	@echo "make docker    build the container image"
	@echo "make compose   build and run with a persistent runs/ volume"

# `setup` installs the dev file, which is `-r requirements.txt` plus the tools,
# so a contributor gets pytest/ruff/mypy in one command exactly as before the
# runtime/dev split. The container does NOT use this target -- the Dockerfile
# installs requirements.txt directly, which is now runtime-only.
setup:
	$(PY) -m pip install -r requirements-dev.txt

setup-runtime:
	$(PY) -m pip install -r requirements.txt

fixtures:
	$(PY) scripts/build_fixtures.py

dev:
	DEMO_MODE=true $(PY) -m uvicorn app.main:app --reload --port 8000

run:
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m pyflakes app scripts tests

# Ruff is configured in pyproject.toml. NEVER add --fix or ruff format here: the
# measured baseline (92 findings) was resolved by dropping and scoping rules, not
# by rewriting source, and an autofixing target would quietly undo that decision.
# Paths are named explicitly -- the same scope `lint` and `type` use -- so a
# scratch .py left at the repo root by an ad-hoc script cannot turn this gate red
# or, worse, get silently excluded from it. Measured state: 0 findings.
ruff:
	$(PY) -m ruff check app scripts tests fixtures pyproject.toml

# Informational, by design. Measured baseline: 36 errors in 9 files (worst:
# app/compare/__init__.py with 26). This prints the number and always exits 0, so
# it is a measurement you can watch go down rather than a gate that blocks every
# commit. To make it a gate, drop the `; echo` and keep the bare command.
type:
	@$(PY) -m mypy app; echo "mypy exit $$? -- baseline, not a gate (36 errors in 9 files, see pyproject.toml)"

# source = ["app"] is set in pyproject.toml, so this measures the application
# package rather than the test suite covering itself. Measured: 90%. The
# fail_under = 88 floor in pyproject.toml lives just below that, so `coverage
# report` below is what enforces it (nonzero exit if coverage regresses).
coverage:
	$(PY) -m coverage run -m pytest tests/ -q
	$(PY) -m coverage report

# Everything CI runs, in one command, cheapest first. `type` is in the list but
# never fails the build (see above); the other three are real gates. Measured
# state: lint 0 findings, ruff 0 findings, mypy 36 errors, test all green.
check: lint ruff type test

probe:
	$(PY) scripts/selfcheck.py

probe-live:
	$(PY) scripts/probe_live.py

# Runs real extractions and diffs them against the hand-authored cache.
# This spends the key (about 2 cents for 6 documents).
probe-diff:
	$(PY) scripts/live_compare.py

capture-summaries:
	$(PY) scripts/capture_summaries.py

migrate:
	$(PY) scripts/migrate_cache_keys.py

docker:
	docker build -t shipment-doc-reconciler .

# One command, with runs/ on a named volume: the filesystem is the database, so
# the container layer is not an acceptable place to keep history.
compose:
	docker compose up --build

# Allowlist, not denylist: .env, runs/, caches and stray artefacts cannot leak
# into the distributed zip because they are never listed.
# Allowlist on purpose: .env, runs/ and dist/ cannot leak because they are
# never named here. Keep it in sync with the submission requirements -- a
# missing LICENSE or SUBMISSION.md is a hard-requirement failure, not a
# cosmetic omission.
DIST_FILES = app web fixtures scripts tests docs \
             requirements.txt requirements-dev.txt requirements.lock.txt \
             pyproject.toml .pre-commit-config.yaml \
             Dockerfile Makefile render.yaml \
             README.md PROGRESS.md SUBMISSION.md VIDEO_SCRIPT.md LICENSE \
             .env.example .gitignore .dockerignore pytest.ini

dist:
	@rm -rf dist && mkdir -p dist
	@staging=$$(mktemp -d); \
	 for item in $(DIST_FILES); do \
	   if [ -e "$$item" ]; then cp -R "$$item" "$$staging/"; fi; \
	 done; \
	 find "$$staging" -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
	 find "$$staging" -name '*.pyc' -delete ; \
	 rm -rf "$$staging/runs" ; \
	 ( cd "$$staging" && zip -qr - . ) > dist/shipment-doc-reconciler.zip ; \
	 rm -rf "$$staging"
	@echo "wrote dist/shipment-doc-reconciler.zip"

clean:
	rm -rf dist runs .pytest_cache .probe_cache .live_runcache
	rm -rf .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
