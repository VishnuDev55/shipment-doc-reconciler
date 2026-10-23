"""Capture the three panels a reviewer asks to see, from a keyless DEMO_MODE run.

Offline by construction: both keys are blanked and DEMO_MODE is on, so the cache
answers every extraction and each report records `live_calls: 0`. Nothing here may
spend a provider call, and nothing here needs one.

    python scripts/make_screenshots.py            # writes docs/screenshots/*.png

Each shot asserts the thing its README caption claims. That is the point of the
assertions: a screenshot whose caption the image does not support is worse than no
screenshot, and it is the kind of lie that quietly survives a refactor. If a UI
change moves a panel or drops the arbitration text, this script fails instead of
publishing a picture of something else.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1280, "height": 960}

# Same shape as tests_e2e/conftest.py BASE_ENV: no key on this machine can reach
# a provider through a screenshot run, and the output is reproducible anywhere.
BASE_ENV = {
    "DEMO_MODE": "true",
    "VISION_PROVIDER": "nvidia",
    "NVIDIA_API_KEY": "",
    "NEBIUS_API_KEY": "",
    "ENABLE_SUMMARY": "true",
    "ENABLE_ADJUDICATION": "false",
    "ENABLE_BRIEFING": "false",
    "MODEL_DISCOVERY": "false",
    "API_KEYS": "",
    "CONFIG_TOKEN": "",
    "ENABLE_LINE_ITEMS": "false",
}

VERDICTS = """() => ['All cross-checked fields agree',
                  'No hard conflicts — a few fields deserve a glance',
                  'Conflicts found — reconcile before the goods move']
             .includes(document.querySelector('#verdict-headline').textContent.trim())"""

# The overlay fades in, and `wait_for(state="visible")` is satisfied at the first
# painted frame -- mid-animation the whole figure is partly transparent, so a
# screenshot taken then photographs the report ghosting through the evidence page.
SETTLED_OVERLAY = """() => {
    const fig = document.querySelector('.lightbox');
    if (!fig) return false;
    const img = fig.querySelector('img');
    return getComputedStyle(fig).opacity === '1'
        && !!img && img.complete && img.naturalWidth > 0;
}"""


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def start_server() -> tuple[subprocess.Popen, str, Path, object]:
    import os

    port = free_port()
    root = ROOT / "runs" / "screenshots"
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        **BASE_ENV,
        "PYTHONIOENCODING": "utf-8",
        "CACHE_DIR": str(ROOT / "fixtures" / "responses"),
        "RUNS_DIR": str(root),
    }
    # The handle has to stay open for the child's whole life, so this cannot be a
    # `with` block; the finally in main() is what releases it.
    log = open(root / "server.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"server exited early (code {proc.returncode}); log:\n"
                f"{(root / 'server.log').read_text(encoding='utf-8', errors='replace')[-2000:]}"
            )
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                return proc, base, root, log
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    proc.terminate()
    raise SystemExit("server never became healthy")


def settled_page(browser, base: str, fixture: str):
    """A fresh context on a finished fixture run — settled means the headline is
    one of the three verdicts, so nothing below races the poll."""
    page = browser.new_context(viewport=VIEWPORT).new_page()
    page.goto(f"{base}/?demo={fixture}", wait_until="domcontentloaded")
    page.wait_for_selector("#results:not([hidden])", timeout=90_000)
    page.wait_for_function(VERDICTS, timeout=90_000)
    return page


def shot_banner(page) -> str:
    """The banner that blocks the shipment, with the arbitration that earns it.

    The card is the shot, not a page-long clip: the CRITICAL row inside it already
    carries the ISO 6346 arbitration sentence, so the banner and its evidence are
    one element. Clipping down to the comparison table instead produced a two
    metre strip whose extra height proved nothing.
    """
    card = page.locator("#status-card")
    card.scroll_into_view_if_needed()
    text = card.inner_text()
    assert "Blocked" in text, f"banner does not read Blocked: {text[:80]!r}"
    assert "ISO 6346" in text and "ARBITRATES" in text, (
        "the banner card no longer carries the container arbitration, so this "
        f"image could not be captioned as showing it: {text[:160]!r}"
    )
    name = "banner-blocked.png"
    card.screenshot(path=str(OUT / name))
    return name


def shot_transparency(page) -> str:
    panel = page.locator("#transparency")
    panel.scroll_into_view_if_needed()
    text = panel.inner_text().lower()
    assert "nvidia" in text, "the transparency panel names no provider"
    assert "cache" in text or "demo" in text, (
        "the panel does not say these answers were replayed, so the image would "
        "read as a live run"
    )
    name = "transparency.png"
    panel.screenshot(path=str(OUT / name))
    return name


def shot_lightbox(page) -> str:
    """Walk the evidence chips until one lands a box on its page.

    Index 0 is not assumed to be the drawn case: which chip carries verbatim
    coordinates depends on the fixture, and shooting the first chip that opens a
    lightbox would happily photograph the honest 'no coordinates' state and let
    the caption claim a rectangle.

    The clip is the stage plus the quoted line, not the `figure` element: the
    lightbox is a fixed full-viewport overlay, so an element screenshot of it is
    a screenshot of the whole dimmed page, which shows nothing.
    """
    chips = page.locator(".page-chip")
    total = chips.count()
    assert total >= 4, f"only {total} evidence chips rendered"
    for index in range(total):
        chips.nth(index).click()
        box = page.locator(".lightbox")
        box.wait_for(state="visible", timeout=10_000)
        if box.locator(".lb-box").count() and box.locator(".lb-evidence").count():
            page.wait_for_function(SETTLED_OVERLAY, timeout=10_000)
            # The stage is the only part worth photographing: it shrinks to the
            # page image, and the measured rectangle is positioned inside it. The
            # figure itself is a fixed full-viewport overlay, so shooting that (or
            # clipping to it) photographs the whole dimmed app instead.
            stage = box.locator(".lb-stage")
            assert stage.locator("img").count() == 1, "the stage holds no page image"
            assert stage.locator(".lb-box").count() == 1, "no rectangle inside the stage"
            name = "evidence-lightbox.png"
            stage.screenshot(path=str(OUT / name))
            page.keyboard.press("Escape")
            return name
        page.keyboard.press("Escape")
    raise SystemExit(
        "no evidence chip landed a rectangle on its page, so the lightbox shot "
        "cannot be taken truthfully — check the text layer, do not caption anyway"
    )


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    proc, base, root, log = start_server()
    written: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for fixture, shots in (
                    ("shipment_conflict", (shot_banner, shot_transparency, shot_lightbox)),
                ):
                    page = settled_page(browser, base, fixture)
                    for shot in shots:
                        written.append(shot(page))
                    page.context.close()
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    for name in written:
        path = OUT / name
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")
    print(f"wrote {len(written)} screenshots to docs/screenshots/ from {base} "
          f"(DEMO_MODE, zero API calls)")
    if len(written) != 3:
        raise SystemExit(f"expected 3 screenshots, wrote {len(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
