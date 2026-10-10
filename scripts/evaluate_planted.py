"""Score the app against the planted-error bundle.

One command, no keys, no network: every response in `fixtures/planted/responses`
is hand-authored, so what is being measured here is the engine -- normalization,
comparison, cross-field rules, arithmetic, evidence checking and the severity
banner -- not a model. `docs/EVAL.md` measures the model, from captured live
responses; this measures what the app concludes when the ground truth is known.

Three numbers matter and they are reported separately on purpose:

  * **caught** -- planted errors the report flagged. A miss is a hole.
  * **false alarms** -- clean and tricky shipments the report did not call ready.
    This is the number that decides whether anyone uses the tool, so it is not
    averaged with the first.
  * **the designed miss** -- one case the engine cannot catch, declared in the
    bundle and explained in the output. A score with no miss in it is a score
    nobody should believe.

Usage:
    python scripts/evaluate_planted.py             # table
    python scripts/evaluate_planted.py --write     # also rewrite docs/PLANTED.md
    python scripts/evaluate_planted.py --check     # exit non-zero on a miss or alarm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.models import Report  # noqa: E402
from app.orchestrator import Reconciler  # noqa: E402
from app.textlayer import load_sidecar  # noqa: E402
from fixtures.planted import CASES  # noqa: E402

PLANTED = ROOT / "fixtures" / "planted"
CACHE = PLANTED / "responses"
DOCS = PLANTED / "index.json"
OUT = ROOT / "docs" / "PLANTED.md"


def _settings(runs_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=True,
        vision_provider="nvidia",
        nvidia_api_key="",
        nebius_api_key="",
        # On, because three of the four arithmetic rules read the goods lines.
        # Left off, this bundle would report those as clean and be wrong about it.
        enable_line_items=True,
        runs_dir=runs_dir,
        cache_dir=CACHE,
    )


def _uploads(case: dict) -> tuple[list, dict]:
    """The case's pages and their text sidecars, as the app would receive them.

    Names come from the generated index rather than from the case, because the
    bundle stores one copy of each distinct page and several cases read the same
    file.
    """
    manifest = json.loads(DOCS.read_text(encoding="utf-8"))["cases"]
    if case["name"] not in manifest:
        raise SystemExit(f"{case['name']} is not in the generated bundle; "
                         "run scripts/build_fixtures.py")
    entries = manifest[case["name"]]["documents"]
    assert len(entries) == len(case["docs"]), (
        f"{case['name']}: the bundle has {len(entries)} pages, the case declares "
        f"{len(case['docs'])}")

    uploads, texts = [], {}
    for entry in entries:
        name = entry["file"]
        path = PLANTED / "images" / name
        if not path.is_file():
            raise SystemExit(f"{name} was never rendered; run build_fixtures.py")
        uploads.append((name, entry["doc_type"], path.read_bytes()))
        side = load_sidecar(PLANTED / "text" / f"{Path(name).stem}.json")
        if side:
            texts[name] = side
    return uploads, texts


# ---------------------------------------------------------------------------
# scoring one case
# ---------------------------------------------------------------------------


def _failed_check_named(report: Report, finding, code: str) -> bool:
    """Did the failing arithmetic rule this case planted actually fail?"""
    return any(c.code == code and c.status == "fail"
               and c.document_label in (finding.documents or [])
               for c in report.internal_checks)


def _matches_wanted_check(report: Report, code: str) -> bool:
    for check in report.internal_checks:
        if check.code == code and check.status == "fail":
            return True
    return any(f.code == code for f in report.sanity)


def _finding_for(report: Report, expect: dict) -> str | None:
    """Return the matched finding's description, or None."""
    findings = report.status_report.findings
    code = expect.get("code")
    if code:
        hit = next((f for f in findings if f.code == code), None)
        if hit is None:
            return None
        if expect.get("level") and hit.severity != expect["level"]:
            return None
        # A code can be shared by several findings -- every failing arithmetic
        # rule reports `math:fail` -- so a case that names a check is only
        # satisfied by that check. Without this, one arithmetic error would
        # score as a pass for all of them.
        check = expect.get("check")
        if check and not _failed_check_named(report, hit, check):
            return None
        return f"{hit.severity} {hit.code}" + (f" ({check})" if check else "")
    check = expect.get("check")
    if check:
        if not _matches_wanted_check(report, check):
            return None
        hit = next((f for f in findings if f.severity in ("critical", "warning")), None)
        return f"{hit.severity} {hit.code}" if hit else f"check {check} failed"
    excluded = expect.get("excluded")
    if excluded:
        if not any(u.field == excluded for u in report.unverified):
            return None
        hit = next((f for f in findings if f.code == "evidence:excluded"), None)
        return f"{hit.severity} {hit.code}" if hit else "withdrawn"
    return None


def score(case: dict, report: Report) -> tuple[bool, str]:
    expect = case["expect"]
    banner = report.status_report.banner
    want = expect["outcome"]

    loud = [f"{f.severity} {f.code}" for f in report.status_report.findings
            if f.severity in ("critical", "warning")]

    if want == "quiet":
        # Nothing about the cargo is wrong; the verdict may still be a review,
        # because a suspicious reading is held by the banner's verdict floor even
        # when its own severity is informational. That is the distinction this
        # case exists to keep honest.
        if not loud:
            return True, f"{banner}, nothing above informational"
        return False, f"{banner}: " + "; ".join(loud)

    if want == "ready":
        if banner == "ready":
            return True, "ready"
        return False, f"banner={banner}: " + ("; ".join(loud) or "no finding named")

    if expect.get("caught") is False:
        # The designed miss: the report must say it is clean, and the script says
        # plainly that this is a hole rather than counting it as a pass.
        return banner == "ready", f"designed miss, banner={banner}"

    if banner != want:
        return False, f"banner={banner}, wanted {want}"
    hit = _finding_for(report, expect)
    if hit is None:
        return False, f"banner right, finding absent (wanted {expect})"
    return True, hit


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


async def _run_all(runs_dir: Path) -> list[dict]:
    results = []
    for case in CASES:
        uploads, texts = _uploads(case)
        rec = Reconciler(settings=_settings(runs_dir / case["name"]))
        started = time.perf_counter()
        sid, _ = await rec.create_shipment(uploads, page_texts=texts)
        await rec.wait(sid, timeout=180)
        report = rec.get(sid)
        elapsed = time.perf_counter() - started
        ok, detail = score(case, report)
        results.append({
            "case": case, "report": report, "ok": ok, "detail": detail,
            "seconds": elapsed,
        })
    return results


def _provenance(results: list[dict]) -> tuple[int, str]:
    """Live calls made, and an honest description of where the values came from."""
    live = sum(r["report"].run.live_calls for r in results)
    sources = {r["report"].run.result_source for r in results}
    return live, "+".join(sorted(sources))


def _by_category(results: list[dict]) -> list[tuple[str, int, int]]:
    out: dict[str, list[int]] = {}
    for r in results:
        row = out.setdefault(r["case"]["category"], [0, 0])
        row[1] += 1
        row[0] += 1 if r["ok"] else 0
    return [(k, v[0], v[1]) for k, v in sorted(out.items())]


def render_table(results: list[dict], live: int, sources: str) -> str:
    groups = ("clean", "tricky", "planted", "miss")
    lines = []
    for group in groups:
        rows = [r for r in results if r["case"]["group"] == group]
        if not rows:
            continue
        lines.append(f"\n{group.upper()}")
        for r in rows:
            mark = "ok  " if r["ok"] else "FAIL"
            lines.append(f"  [{mark}] {r['case']['name']:28} "
                         f"{r['detail'][:58]:58} {r['seconds']:5.2f}s")

    planted = [r for r in results if r["case"]["group"] == "planted"]
    alarms = [r for r in results
              if r["case"]["group"] in ("clean", "tricky") and not r["ok"]]
    caught = sum(1 for r in planted if r["ok"])
    total = sum(r["seconds"] for r in results)

    lines.append("")
    lines.append(f"planted errors caught   {caught}/{len(planted)}")
    lines.append(f"false alarms            {len(alarms)}/"
                 f"{sum(1 for r in results if r['case']['group'] in ('clean', 'tricky'))}"
                 "  (clean + tricky shipments not called ready)")
    lines.append(f"designed miss           "
                 f"{sum(1 for r in results if r['case']['group'] == 'miss')} declared")
    lines.append(f"wall clock              {total:.1f}s for {len(results)} shipments "
                 f"({sum(len(c['docs']) for c in CASES)} documents)")
    lines.append(f"API calls made          {live}  (provenance: {sources})")
    lines.append("approximate cost        $0.00 -- every response is hand-authored, "
                 "so this run measures the engine, not a model")
    return "\n".join(lines)


def render_markdown(results: list[dict], live: int, sources: str) -> str:
    planted = [r for r in results if r["case"]["group"] == "planted"]
    quiet = [r for r in results if r["case"]["group"] in ("clean", "tricky")]
    alarms = [r for r in quiet if not r["ok"]]
    caught = sum(1 for r in planted if r["ok"])
    misses = [r for r in planted if not r["ok"]]
    lines = [
        "# Planted-error evaluation",
        "",
        "Generated by `python scripts/evaluate_planted.py --write`. Do not edit by",
        "hand: the numbers are a measurement, and the day they stop matching the",
        "code is the day this file should change.",
        "",
        f"{len(CASES)} synthetic shipments built from `fixtures/planted.py`, where",
        "every case declares its ground truth before anything runs. Pages, their",
        "text layers and the model responses are all hand-authored, so **zero API",
        "calls** are made and the score describes the engine -- normalization,",
        "comparison, cross-field rules, arithmetic, evidence checking and the",
        "severity banner -- not a model's mood.",
        "",
        f"- planted errors caught: **{caught}/{len(planted)}**",
        f"- false alarms on clean or tricky paperwork: **{len(alarms)}/{len(quiet)}**",
        f"- designed misses declared in the bundle: "
        f"**{sum(1 for r in results if r['case']['group'] == 'miss')}**",
        f"- documents: {sum(len(c['docs']) for c in CASES)} slots over "
        f"{len({d['file'] for case in json.loads(DOCS.read_text(encoding='utf-8'))['cases'].values() for d in case['documents']})} "
        f"distinct pages, wall clock {sum(r['seconds'] for r in results):.1f}s",
        f"- API calls: {live} (provenance: {sources})",
        "",
        "## Per case",
        "",
        "| Case | Group | Category | Expected | Result | Why |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        case = r["case"]
        lines.append(
            f"| `{case['name']}` | {case['group']} | {case['category']} | "
            f"`{case['expect']['outcome']}` | "
            f"{'✅' if r['ok'] else '❌'} {r['detail']} | {case['note']} |")

    lines += ["", "## By category", "", "| Category | Passed | Cases |", "|---|---|---|"]
    for name, ok, total in _by_category(results):
        lines.append(f"| {name} | {ok} | {total} |")

    lines += ["", "## What this bundle cannot see", ""]
    for r in results:
        if r["case"]["group"] == "miss":
            lines.append(f"- **`{r['case']['name']}`** -- {r['case']['note']}")
    for r in misses:
        lines.append(f"- **`{r['case']['name']}`** -- expected "
                     f"`{r['case']['expect']['outcome']}`, the report said "
                     f"{r['detail']}. This is a defect, not a limit.")
    lines += [
        "",
        "Two limits sit outside the bundle's reach and are stated rather than "
        "implied. A value that is wrong on every document -- all four papers "
        "copying the same bad figure -- has nothing in the paperwork to "
        "contradict it, and only the arithmetic checks catch that when the "
        "documents disagree with *themselves*. And invention on a photo or scan "
        "is undetectable by code: there is no text layer to check a quote "
        "against, which is why `planted_invented_value` needs a sidecar to work.",
        "",
        "## Reproducing",
        "",
        "```bash",
        "python scripts/build_fixtures.py       # regenerates fixtures/planted/",
        "python scripts/evaluate_planted.py     # the table",
        "python scripts/evaluate_planted.py --write    # rewrite this file",
        "python scripts/evaluate_planted.py --check    # fail on a miss or an alarm",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite docs/PLANTED.md")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if a planted error is missed or clean paperwork alarms")
    args = parser.parse_args()

    if not CACHE.is_dir() or not DOCS.is_file():
        print("fixtures/planted/ is missing; run scripts/build_fixtures.py first",
              file=sys.stderr)
        return 2

    runs_dir = ROOT / "_planted_runs"
    shutil.rmtree(runs_dir, ignore_errors=True)
    try:
        results = asyncio.run(_run_all(runs_dir))
    finally:
        shutil.rmtree(runs_dir, ignore_errors=True)

    live, sources = _provenance(results)
    print(render_table(results, live, sources))

    if args.write:
        OUT.write_text(render_markdown(results, live, sources), encoding="utf-8")
        print(f"\nwrote {OUT.relative_to(ROOT)}")

    if args.check:
        bad = [r for r in results if not r["ok"] and r["case"]["group"] != "miss"]
        if bad:
            print(f"\nCHECK FAILED: {len(bad)} case(s) scored wrong: "
                  + ", ".join(r["case"]["name"] for r in bad), file=sys.stderr)
            return 1
        if live:
            print("\nCHECK FAILED: the bundle made live API calls", file=sys.stderr)
            return 1
        print("\ncheck passed: every planted error flagged, no false alarms, "
              "no API calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
