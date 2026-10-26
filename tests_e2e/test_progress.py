"""What the app says while it is working, and what it stops saying when it fails.

A vision read takes 30-90 s per document, so the interesting properties are not
cosmetic: that a click is acknowledged in the same frame, that every file has a
state rather than an indefinite spinner, that a failure ends the animation
instead of leaving it running, and that none of it moves the layout or ignores
`prefers-reduced-motion`.

The upload runs are real (DEMO_MODE, replayed from the committed cache, no API
calls). The stage events are driven through `window.applyStage`, the same
handler the stream feeds, because a cached run finishes faster than a browser
can watch it -- the existing timeline test does the same.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import SETTLED_VERDICT

REPO = Path(__file__).resolve().parents[1]
IMAGES = REPO / "fixtures" / "images"

TWO_FILES = [str(IMAGES / "invoice_clean.png"), str(IMAGES / "packing_list_clean.png")]


def _choose(page, files=None):
    page.wait_for_selector("#dropzone", timeout=20_000)
    page.set_input_files("#file-input", files or TWO_FILES)
    page.wait_for_function("() => !document.querySelector('#analyze').disabled",
                           timeout=15_000)


def _settle(page):
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_function(SETTLED_VERDICT, timeout=60_000)


def test_the_button_answers_the_click_before_the_network_does(page, app):
    """Break caught on the live instance: on a cold free-tier instance a click sat
    there doing nothing for seconds, because the only feedback was a class that
    faded the button. The state has to change in the click's own frame.

    Timed from the click event's own `timeStamp`, not from a listener added after
    the app's: the app's handler runs first and disables the button before our
    listener gets the event, so measuring there would report the handler's work
    as latency it did not have.
    """
    page.goto(app.base_url + "/")
    _choose(page)
    page.evaluate(
        """() => {
            window.__btn = { t0: null, first: null, states: [] };
            document.addEventListener('click', (e) => {
                if (e.target.closest('#analyze')) window.__btn.t0 = e.timeStamp;
            }, true);
            const btn = document.querySelector('#analyze');
            new MutationObserver(() => {
                window.__btn.states.push({
                    disabled: btn.disabled,
                    label: document.querySelector('#analyze-label').textContent,
                    busy: btn.classList.contains('busy'),
                });
                if (window.__btn.first === null) {
                    window.__btn.first = performance.now() - window.__btn.t0;
                }
            }).observe(btn, { attributes: true, attributeFilter: ['disabled'] });
        }""")
    page.click("#analyze")
    page.wait_for_function("() => window.__btn.first !== null", timeout=10_000)
    data = page.evaluate("() => window.__btn")
    assert data["states"][0] == {"disabled": True, "label": "Uploading…", "busy": True}, \
        data["states"][0]
    assert data["first"] < 100, f"the button took {data['first']:.0f} ms to answer"

    _settle(page)
    page.wait_for_function(
        "() => window.__btn.states.some((s) => !s.disabled)", timeout=30_000)
    assert page.locator("#analyze-label").inner_text() == "Analyse shipment"
    assert not page.errors, page.errors


def test_every_file_says_what_is_happening_to_it(page, app):
    """Two files, two states, and a combined bar that is a measurement: the line
    must reach '2 of 2 files uploaded' and every row must end settled rather than
    spinning for ever."""
    page.goto(app.base_url + "/")
    _choose(page)
    page.evaluate(
        """() => {
            window.__up = { lines: [], pct: [] };
            const line = document.querySelector('#upload-line');
            const bar = document.querySelector('#upload-bar');
            new MutationObserver(() => window.__up.lines.push(line.textContent))
                .observe(line, { childList: true, characterData: true, subtree: true });
            new MutationObserver(() => window.__up.pct.push(bar.getAttribute('aria-valuenow')))
                .observe(bar, { attributes: true, attributeFilter: ['aria-valuenow'] });
        }""")
    page.click("#analyze")

    assert page.locator("#upload-list .upload").count() == 2
    _settle(page)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#upload-list .upload-state')]"
        ".every((s) => s.dataset.state === 'done')", timeout=30_000)

    up = page.evaluate("() => window.__up")
    assert "2 of 2 files uploaded" in up["lines"], up["lines"]
    assert "100" in up["pct"], up["pct"]
    # Settled means quiet: the upload bar and the stage line are gone.
    assert page.locator("#upload-progress").is_hidden()
    assert page.locator("#stage-line").inner_text() == ""
    assert not page.errors, page.errors


def test_the_stage_line_says_which_document_and_which_model(page, app):
    """One plain sentence is the thing a reader glances at; it must name the
    document, the model, the host and the seconds, and the row keeps its pulsing
    dot while the work is live."""
    page.goto(app.base_url + "/")
    page.wait_for_selector("#empty:not([hidden])")
    page.evaluate("window.showResults()")
    page.evaluate(
        "window.applyStage({document_id:'d1', document_label:'invoice.png', "
        "stage:'reading', provider:'nvidia', "
        "model:'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning', elapsed_ms:0})")
    line = page.locator("#stage-line").inner_text()
    assert "Reading invoice.png" in line, line
    assert "nemotron" in line and "NVIDIA" in line, line
    assert line.rstrip().endswith("s"), line

    page.evaluate(
        "window.applyStage({document_id:'d1', document_label:'invoice.png', "
        "stage:'verifying', provider:'nvidia', "
        "model:'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning', elapsed_ms:23000})")
    line = page.locator("#stage-line").inner_text()
    assert "Verifying evidence invoice.png" in line, line
    assert "23 s" in line, line

    icon = page.locator(".tl-icon.tl-icon-verifying").first
    assert icon.evaluate("(n) => getComputedStyle(n).animationName") == "pulse"


def test_a_failed_upload_stops_the_animation_and_offers_retry(page, app):
    """A 429 from the daily cap is the failure a public deployment actually hits.
    The message has to be the server's own words (it names the cap and the reset
    time), nothing may still be spinning, and the retry has to be a real button."""
    detail = "Daily spend cap reached ($2.00). Uploads resume after 2026-10-27T00:00Z."
    calls = {"n": 0}

    def handler(route):
        # The runs list is a GET on the same path; only the upload is the subject.
        if route.request.method != "POST":
            route.continue_()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=429, content_type="application/json",
                          body=json.dumps({"detail": detail}))
        else:
            route.continue_()

    page.route("**/api/shipments", handler)
    page.goto(app.base_url + "/")
    _choose(page)
    page.click("#analyze")
    page.wait_for_selector("#error:not([hidden])", timeout=20_000)

    assert detail in page.locator("#error-text").inner_text()
    assert page.locator("#error-retry").is_visible()
    assert page.evaluate(
        "() => document.activeElement === document.querySelector('#error-retry')"), (
        "focus must land on the retry, not stay on a disabled button")
    assert not page.locator("#analyze").is_disabled()
    assert page.locator("#analyze-label").inner_text() == "Analyse shipment"
    assert page.locator("#analyze").evaluate(
        "(n) => !n.classList.contains('busy')")
    assert page.locator("#upload-progress").is_hidden()
    states = page.evaluate(
        "() => [...document.querySelectorAll('#upload-list .upload-state')]"
        ".map((s) => s.dataset.state)")
    assert states and set(states) == {"failed"}, states

    # Retry goes through to the real (demo) server and the run settles.
    page.locator("#error-retry").click()
    _settle(page)
    assert page.locator("#error").is_hidden()
    assert not page.errors, page.errors


def test_a_waking_server_says_so_after_four_seconds(page, app):
    """A free Render instance can take a minute to answer its first request. A
    silent page for that minute is the difference between "slow" and "broken".

    The health reply is held open and then served from the real one, so the page
    meets a slow server rather than a made-up one. Held, not delayed inside the
    handler: a sleeping handler runs during `goto` and would answer before the
    test could look at the page.
    """
    health = app.json("/api/health")
    held = []

    def handler(route):
        if route.request.method != "GET":
            route.continue_()
            return
        held.append(route)  # answered below, after the note has been seen

    page.route("**/api/health", handler)
    page.goto(app.base_url + "/")
    note = page.locator("#cold-start")
    note.wait_for(state="visible", timeout=10_000)   # the app waits four seconds
    assert "waking up" in note.inner_text().lower()
    assert "minute" in note.inner_text().lower()
    assert page.locator("#server-state-text").inner_text() == "Server is waking up…"
    assert held, "the health request was never intercepted"

    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(health))
    page.wait_for_function(
        "() => document.querySelector('#server-state-text').textContent === 'Server ready'",
        timeout=15_000)
    assert note.is_hidden(), "the waking note outlived the wake-up"
    dot = page.locator("#srv-dot")
    assert "ready" in (dot.get_attribute("class") or "")
    assert not page.errors, page.errors


def test_reduced_motion_switches_the_loops_off(new_page, app):
    """Only two things loop in this UI. A reader who asked the OS for less motion
    must not get a pulsing dot, a shimmering bar or a spinning button."""
    page = new_page(reduced_motion="reduce")
    page.goto(app.base_url + "/")
    page.wait_for_selector("#empty:not([hidden])")
    page.evaluate("window.showResults()")
    page.evaluate(
        "window.applyStage({document_id:'d1', document_label:'invoice.png', "
        "stage:'reading', provider:'nvidia', model:'nvidia/x', elapsed_ms:1000})")
    page.evaluate(
        """() => {
            const box = document.querySelector('#upload-progress');
            box.hidden = false; box.classList.add('uploading');
        }""")
    names = page.evaluate(
        """() => ({
            pulse: getComputedStyle(document.querySelector('.tl-icon-reading')).animationName,
            shimmer: getComputedStyle(document.querySelector('.up-progress.uploading .up-bar'))
                .animationName,
            spinner: getComputedStyle(document.querySelector('#analyze-spinner')).animationName,
            dot: getComputedStyle(document.querySelector('#srv-dot')).animationName,
        })""")
    assert names["pulse"] == "none", names
    assert names["shimmer"] == "none", names
    assert names["spinner"] == "none", names
    assert names["dot"] == "none", names
    page.close()


def test_the_progress_ui_fits_a_phone(new_page, app):
    """390px, two rows of file state, a long model line: the page may not grow a
    horizontal scrollbar, which would put the header and the button off-screen.

    Driven through the same handler the stream calls rather than by racing a
    cached run, which settles before a row can be photographed.
    """
    page = new_page(viewport={"width": 390, "height": 844})
    page.goto(app.base_url + "/")
    _choose(page)
    page.evaluate("window.showResults()")
    page.evaluate(
        "window.applyStage({document_id:'d1', document_label:'invoice_clean.png', "
        "stage:'reading', provider:'nvidia', "
        "model:'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning', elapsed_ms:12345})")
    page.wait_for_selector(".timeline-row", state="visible", timeout=10_000)

    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth"), \
        "the progress UI widened the page on a phone"
    for selector in ("#analyze", ".upload", ".timeline-row", "#stage-line"):
        rect = page.locator(selector).first.bounding_box()
        assert rect and rect["x"] >= 0 and rect["x"] + rect["width"] <= 390, (
            selector, rect)
    assert not page.errors, page.errors
    page.close()


def test_the_progress_ui_follows_the_dark_theme(new_page, app):
    """The panels are theme-aware by construction: every colour is a variable.
    This pins that the new widgets did not smuggle in a light-only colour."""
    page = new_page(color_scheme="dark")
    page.goto(app.base_url + "/")
    _choose(page)
    page.click("#analyze")
    page.wait_for_selector("#upload-progress:not([hidden])", timeout=20_000)
    bg = page.evaluate(
        """() => {
            const parts = getComputedStyle(document.body).backgroundColor.match(/\\d+/g);
            return parts ? parts.slice(0, 3).map(Number) : [255, 255, 255];
        }""")
    assert sum(bg) < 300, f"the dark theme did not apply: {bg}"
    ink = page.evaluate(
        "() => getComputedStyle(document.querySelector('#upload-line')).color")
    assert ink and ink != "rgba(0, 0, 0, 0)", ink
    _settle(page)
    assert not page.errors, page.errors
    page.close()
