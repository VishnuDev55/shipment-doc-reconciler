"""Documentation claims that were hand-typed once are the ones that rot.

The README used to carry every number in this project inline, and it drifted:
"six fields" after the contract grew to ten, "nine fixture documents" after the
cache reached twelve, "docker build has never been executed" after CI started
executing it. Each of those is a sentence that was true and then simply was not,
with nothing in the suite noticing.

So the sentences that can be derived are derived here, from `fixtures/`,
`app/fields.py` and the filesystem. A claim that survives a refactor of the docs
is a claim that was tied to something other than prose.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("evaluate", REPO / "scripts" / "evaluate.py")
assert _spec and _spec.loader
evaluate = importlib.util.module_from_spec(_spec)
sys.modules["evaluate"] = evaluate
_spec.loader.exec_module(evaluate)

FRONT_DOOR = REPO / "README.md"
DOC_DIR = REPO / "docs"
SCREENSHOTS = DOC_DIR / "screenshots"
ROOT_DOCS = [REPO / name for name in ("DEMO_SCRIPT.md", "CONTRIBUTING.md", "SUBMISSION.md")]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def results() -> list:
    return evaluate.run()


def test_the_readme_is_a_front_door_not_the_whole_house() -> None:
    """The whole point of the split. Bounded above so it cannot creep back to
    fifteen hundred lines, and below so it stays a real introduction."""
    lines = _text(FRONT_DOOR).splitlines()
    assert 150 <= len(lines) <= 220, f"README.md is {len(lines)} lines"


def test_the_deep_detail_lives_in_the_docs_the_readme_links_to() -> None:
    for name in ("FEATURES.md", "VERIFICATION.md", "LIVE_FINDINGS.md",
                 "ARCHITECTURE.md", "NORMALIZATION.md", "PROVIDERS.md"):
        assert (DOC_DIR / name).is_file(), name
        assert f"docs/{name}" in _text(FRONT_DOOR), f"README no longer links {name}"


# ---------------------------------------------------------------------------
# claims that must be derived, never remembered
# ---------------------------------------------------------------------------


def _cache_provenance() -> tuple[int, int]:
    authored = live = 0
    for path in (REPO / "fixtures" / "responses").glob("*.json"):
        marker = str(json.loads(_text(path)).get("_provenance", "")).upper()
        if "HAND-AUTHORED" in marker:
            authored += 1
        elif marker:
            live += 1
    return authored, live


def _fixture_documents() -> int:
    index = json.loads(_text(REPO / "fixtures" / "index.json"))
    return sum(len(ship["documents"]) for ship in index["shipments"].values())


def test_the_layout_block_counts_the_cache_that_exists() -> None:
    """`responses/ N entries / M documents` is the sentence a re-capture breaks
    first, because the cache grows and the prose does not."""
    authored, live = _cache_provenance()
    documents = _fixture_documents()
    layout = _text(DOC_DIR / "ARCHITECTURE.md")
    assert f"{authored + live} entries / {documents} documents" in layout
    assert f"{live} " in layout, "the live-capture count is missing from the layout"
    assert f"{authored} hand-authored" in layout


def test_every_captured_provider_is_named_in_the_layout() -> None:
    models = {
        json.loads(_text(p)).get("model")
        for p in (REPO / "fixtures" / "responses").glob("*.json")
        if "HAND-AUTHORED" not in str(json.loads(_text(p)).get("_provenance", ""))
    }
    layout = _text(DOC_DIR / "ARCHITECTURE.md")
    for tag in ("omni live", "MiniCPM-V live", "Super-VL live"):
        assert any(tag.split()[0].lower() in str(m).lower() for m in models), tag
        assert tag in layout, f"the cache breakdown lost {tag!r}"


def test_no_doc_still_claims_the_docker_build_was_never_run() -> None:
    """CI has built this image on every push since 2026-10-23. The old sentence
    was true, then the workflow ran, and nothing noticed."""
    stale = ("has not been executed", "never run — no Docker", "build itself is unconfirmed",
             "never executed")
    for path in [FRONT_DOOR, *sorted(DOC_DIR.glob("*.md")), *ROOT_DOCS]:
        text = _text(path)
        for phrase in stale:
            assert phrase not in text, f"{path.name} still says {phrase!r}"


def test_the_field_count_is_the_contract_not_a_remembered_number() -> None:
    from app.fields import FIELD_ORDER

    assert len(FIELD_ORDER) == 10
    # Whitespace collapsed: docs wrap at ~80 columns, and a test that depends on
    # where a line breaks would fail for reasons that have nothing to do with the
    # claim it is checking.
    combined = " ".join(
        " ".join(_text(p).split())
        for p in [FRONT_DOOR, *sorted(DOC_DIR.glob("*.md"))])
    assert "six fixed names" not in combined
    assert "Six scalar fields" not in combined
    assert "ten fixed names" in combined, "the docs should name the count they mean"


def test_the_docs_invented_count_matches_the_capture(results: list) -> None:
    """This sentence moved out of the README, and the rule moved with it: the
    word for the count comes from the captures, not from whoever last edited a
    paragraph."""
    by_provider = {r.provider: r for r in results}
    words = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
    expected = f"Every one of the {words[by_provider['nebius'].total.invented]} fields"
    text = _text(DOC_DIR / "FEATURES.md")
    assert expected in text, f"docs/FEATURES.md must say {expected!r}"


def test_no_doc_claims_a_hosted_instance_exists() -> None:
    """Nothing is deployed. The wording that used to slip was the casual
    "what the hosted instance runs", which reads as a fact about a URL."""
    for path in [FRONT_DOOR, *sorted(DOC_DIR.glob("*.md")), REPO / "SUBMISSION.md"]:
        text = _text(path).lower()
        assert "the hosted instance runs" not in text, path.name
        assert "hosted submission instance" not in text, path.name


# ---------------------------------------------------------------------------
# the screenshots the README shows
# ---------------------------------------------------------------------------


def test_every_screenshot_exists_and_is_referenced_from_the_readme() -> None:
    pngs = sorted(SCREENSHOTS.glob("*.png"))
    assert len(pngs) == 3, f"expected 3 screenshots, found {[p.name for p in pngs]}"
    readme = _text(FRONT_DOOR)
    for path in pngs:
        assert path.stat().st_size > 20_000, f"{path.name} is too small to be a UI shot"
        assert f"screenshots/{path.name}" in readme, f"{path.name} is not linked"
        assert readme.count(f"screenshots/{path.name}") == 1, f"{path.name} linked twice"


def test_the_screenshot_generator_is_offline_by_construction() -> None:
    """A script that writes the README's evidence images must not be able to
    spend a provider key."""
    source = _text(REPO / "scripts" / "make_screenshots.py")
    assert '"DEMO_MODE": "true"' in source
    assert '"NVIDIA_API_KEY": ""' in source
    assert '"NEBIUS_API_KEY": ""' in source


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def _slug(heading: str) -> str:
    text = re.sub(r"^#+\s*", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def _anchors(path: Path) -> set[str]:
    out, fence = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and line.startswith("#"):
            out.add(_slug(line))
    return out


_LINK = re.compile(r"\]\((<?)([^)\s>]+)>?\)")


def test_every_relative_link_in_the_docs_resolves() -> None:
    broken: list[str] = []
    for path in [FRONT_DOOR, *sorted(DOC_DIR.glob("*.md")), *ROOT_DOCS]:
        for _, target in _LINK.findall(_text(path)):
            if target.startswith(("http://", "https://", "mailto:", "<")):
                continue
            file_part, _, anchor = target.partition("#")
            if file_part:
                resolved = (path.parent / file_part).resolve()
                if not resolved.exists():
                    broken.append(f"{path.name} -> {target}")
                    continue
            else:
                resolved = path
            if anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                broken.append(f"{path.name} -> {target} (no such heading)")
    assert not broken, "broken links: " + ", ".join(broken)


def test_every_test_module_is_listed_in_the_coverage_table() -> None:
    """The table used to name 14 files while the suite had 38, which made it read
    as a summary when it was actually a stale list."""
    table = _text(DOC_DIR / "VERIFICATION.md")
    missing = [p.name for p in sorted((REPO / "tests").glob("test_*.py"))
               if p.name not in table]
    assert not missing, f"tests not in docs/VERIFICATION.md: {missing}"
