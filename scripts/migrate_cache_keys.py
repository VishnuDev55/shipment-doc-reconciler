"""One-time migration: re-key cache entries that predate provider-aware keys.

The cache key originally hashed (prompt_version, model, page_hashes). It now
hashes (prompt_version, provider, model, page_hashes) so a response served by
NVIDIA build.nvidia.com can never be replayed as one served by Nebius Token
Factory. Entries written before that change therefore have a filename that no
longer matches the key used to look them up, which would silently discard real
captured model output.

Idempotent: running it twice does nothing the second time.

This script re-keys, it does not re-extract. An entry captured under an older
PROMPT_VERSION asked for a different set of fields, so giving it a new key would
be a false claim that it satisfies the new contract -- the reader would then be
handed a six-field response for a ten-field question. Those entries are left
alone with an instruction to re-capture.

    python scripts/migrate_cache_keys.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._common import ROOT  # noqa: E402
from app.cache import AUTHORED_MARKER, cache_key  # noqa: E402
from app.config import PROMPT_VERSION  # noqa: E402

CACHE_DIR = ROOT / "fixtures" / "responses"

# Every pre-existing *captured* response was served by Nebius Token Factory;
# the NVIDIA path did not exist when they were written. This is only ever
# applied to entries that carry a model response, never to authored fixtures.
LEGACY_PROVIDER = "nebius"


def main() -> int:
    if not CACHE_DIR.is_dir():
        print(f"nothing to migrate: {CACHE_DIR} does not exist")
        return 0

    migrated = skipped = broken = stale = 0
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"  unreadable, leaving in place: {path.name}")
            broken += 1
            continue

        provider = payload.get("provider")
        pages = payload.get("page_sha256") or []
        model = payload.get("model") or ""
        if not pages:
            print(f"  no page_sha256, cannot re-key: {path.name}")
            broken += 1
            continue

        if AUTHORED_MARKER in str(payload.get("_provenance", "")).upper():
            # Hand-authored fixtures are keyed on a label, not a provider, and
            # are looked up by page hashes at read time precisely so they stay
            # valid for whichever host is configured. Attaching LEGACY_PROVIDER
            # to one would be a guess about a file that has no model behind it.
            print(f"  authored fixture, left as-is: {path.name}")
            skipped += 1
            continue

        recorded = str(payload.get("prompt_version") or "")
        if recorded and recorded != PROMPT_VERSION:
            # A different extraction contract, not a different filename. Re-keying
            # it would advertise old output as satisfying the current schema.
            print(f"  left alone: captured under {recorded}, this build sends "
                  f"{PROMPT_VERSION}. Re-run scripts/live_compare.py to re-capture.")
            stale += 1
            continue

        if provider:
            # Already migrated: verify the filename still matches its contents.
            expected = cache_key(pages, model, provider)
            if expected == path.stem:
                skipped += 1
                continue
            target = CACHE_DIR / f"{expected}.json"
            if target.exists() and target != path:
                print(f"  COLLISION leaving both: {path.name} vs {target.name}")
                broken += 1
                continue
            payload["previous_key"] = path.stem
            path.write_text(json.dumps(payload, indent=2))
            path.rename(target)
            migrated += 1
            continue

        payload["provider"] = LEGACY_PROVIDER
        new_key = cache_key(pages, model, LEGACY_PROVIDER)
        payload["cache_key"] = new_key
        payload["previous_key"] = path.stem
        payload["_migration"] = f"provider-key migration; was {path.stem}"
        target = CACHE_DIR / f"{new_key}.json"
        if target.exists() and target != path:
            print(f"  target exists, leaving {path.name} untouched")
            broken += 1
            continue
        path.write_text(json.dumps(payload, indent=2))
        path.rename(target)
        migrated += 1

    print(f"\nmigrated={migrated} already-current={skipped} stale-contract={stale} needs-attention={broken}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
