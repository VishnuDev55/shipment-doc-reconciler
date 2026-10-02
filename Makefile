PY := python3

.PHONY: help setup fixtures dev run test lint probe probe-live probe-diff capture-summaries migrate dist clean docker

help:
	@echo "make setup     install dependencies"
	@echo "make fixtures  (re)generate synthetic shipments + DEMO_MODE response cache"
	@echo "make dev       run the app in DEMO_MODE with autoreload"
	@echo "make run       run the app (reads .env; set DEMO_MODE=false for live)"
	@echo "make test      run the test suite (zero API calls)"
	@echo "make lint      pyflakes over app/ scripts/ tests/"
	@echo "make probe     self-check the demo pipeline end to end"
	@echo "make probe-live     verify BOTH providers live (vision + Nebius summary)"
	@echo "make probe-diff     live extractions diffed vs the authored cache"
	@echo "make capture-summs  capture real AI summaries for the demo cache"
	@echo "make migrate        re-key cache entries after a cache-format change"
	@echo "make dist      write a clean source zip to dist/"
	@echo "make docker    build the container image"

setup:
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

# Allowlist, not denylist: .env, runs/, caches and stray artefacts cannot leak
# into the distributed zip because they are never listed.
# Allowlist on purpose: .env, runs/ and dist/ cannot leak because they are
# never named here. Keep it in sync with the submission requirements -- a
# missing LICENSE or SUBMISSION.md is a hard-requirement failure, not a
# cosmetic omission.
DIST_FILES = app web fixtures scripts tests \
             requirements.txt Dockerfile Makefile render.yaml \
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
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
