"""The UI, rendered in a real browser, checked against the claims it makes.

These are not pixel tests. They cover what the Python suite cannot see because it
only exists once the DOM is built: that a panel is *hidden* rather than empty,
that a demo run's provenance line does not imply a call was made, that the
goods-lines section stays out of the way when nobody asked for it, and that a
protected instance can be unlocked from the browser without the key being left
behind in storage.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

FIELD_COUNT = 10  # app/fields.py: the table has to show every contracted field

KEY = "e2e-secret-key-42"


def _api(server, path, payload=None, method=None):
    """Reach the running server the way a reviewer's browser does."""
    url = server.base_url + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _shipment_id(page):
    href = page.locator("#export").get_attribute("href") or ""
    parts = href.rstrip("/").split("/")
    return parts[-2] if parts[-1] == "export" else ""


# ---------------------------------------------------------------------------
# boot and the golden path
# ---------------------------------------------------------------------------


def test_the_app_boots_without_a_script_error_and_shows_the_empty_state(page, app):
    page.goto(app.base_url + "/")
    page.wait_for_selector("#empty:not([hidden])")
    assert page.locator("#results").is_hidden()
    assert page.errors == [], f"uncaught errors: {page.errors}"


def test_cold_start_announces_demo_mode_before_anything_is_reconciled(page, app):
    """First load in DEMO_MODE has to say what is about to happen: the responses
    are captured real model output being replayed, not a live call and not a
    fabrication. The banner is the very first element in <main>, so a cold
    start cannot miss it."""
    page.goto(app.base_url + "/")
    banner = page.locator("#demo-banner")
    banner.wait_for(timeout=10_000)
    assert banner.inner_text().strip() == "Demo mode: replaying captured real model output"
    assert banner.is_visible()


def test_a_sample_shipment_renders_a_settled_verdict(report_page):
    assert "Conflicts found" in report_page.locator("#verdict-headline").inner_text()
    assert report_page.locator("#counts button").count() >= 4
    assert report_page.errors == []


def test_every_contracted_field_reaches_the_comparison_table(report_page):
    report_page.locator("#cmp tbody tr").first.wait_for(timeout=10_000)
    shown = report_page.locator("#cmp tbody tr").count()
    assert shown >= FIELD_COUNT, f"only {shown} of {FIELD_COUNT} fields rendered"


def test_the_risk_strip_shows_its_arithmetic_not_just_a_number(report_page):
    risk = report_page.locator("#risk")
    assert risk.is_visible(), "the conflict fixture should raise a risk reason"
    assert "/100" in risk.locator("#risk-level").inner_text()
    assert risk.locator("#risk-contrib li").count() >= 1
    # The number has to be attributable to rules, not to a model's judgement.
    assert "no model was asked" in risk.locator("#risk-src").inner_text()


# ---------------------------------------------------------------------------
# honesty of the provenance text
# ---------------------------------------------------------------------------


def test_a_demo_run_never_claims_a_live_call(report_page):
    sub = report_page.locator("#verdict-sub").inner_text()
    assert ("replayed from cache" in sub or "demo cache" in sub
            or "hand-authored" in sub), sub
    assert "on this run" not in sub, sub
    assert "cache" in report_page.locator("#transparency").inner_text().lower()


def test_the_powered_by_badge_is_honest_in_demo_mode(report_page):
    badge = report_page.locator("#transparency .badge-powered").inner_text()
    assert "Powered by Nebius Token Factory" in badge
    assert "no live call" in badge


def test_the_config_drawer_exposes_the_second_reader_toggle(page, app):
    page.goto(app.base_url + "/")
    page.locator("#open-config").click()
    toggle = page.locator("#cfg-consensus")
    toggle.wait_for(timeout=10_000)
    assert toggle.is_visible()
    assert not toggle.is_checked()  # consensus is opt-in, off by default


def test_a_stage_event_renders_the_per_document_timeline(page, app):
    """The SSE stage frames feed a per-document timeline with the provider/model
    and the elapsed seconds. Driving the handler directly keeps this offline and
    deterministic — a cached demo run finishes faster than a browser can watch it.
    """
    page.goto(app.base_url + "/")
    page.wait_for_selector("#empty:not([hidden])")

    # A real run flips #results visible before the first stage event lands.
    page.evaluate("window.showResults()")
    page.evaluate(
        "window.applyStage({document_id:'doc1', document_label:'invoice.png', "
        "stage:'reading', provider:'nvidia', model:'nvidia/nemotron', elapsed_ms:1234})"
    )

    timeline = page.locator("#timeline")
    timeline.wait_for(timeout=10_000)
    assert timeline.is_visible()
    text = timeline.inner_text().lower()
    assert "invoice.png" in text
    assert "reading" in text
    assert "nvidia" in text and "nvidia/nemotron" in text
    assert "1.2 s" in text


def test_the_goods_lines_panel_is_absent_when_the_feature_was_never_asked_for(report_page):
    """Off by default, and the screen must not show a check that did not happen.

    An empty "Goods lines" card reads as "no line disagrees" — a claim about the
    documents rather than about a configuration flag.
    """
    assert report_page.locator("#lineitems-card").is_hidden()


def test_an_opt_in_run_that_cannot_read_lines_says_why(start_server, new_page):
    server = start_server(enable_line_items="true")
    page = new_page()
    page.goto(server.base_url + "/?demo=shipment_conflict")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_selector("#lineitems-card:not([hidden])", timeout=60_000)

    assert page.locator("#lineitems tbody tr").count() == 0
    note = page.locator("#lineitems-note").inner_text()
    assert "no captured line-item response" in note, note
    assert "no goods lines were read" in note, note


# ---------------------------------------------------------------------------
# evidence and corrections
# ---------------------------------------------------------------------------


def test_a_page_chip_opens_the_page_with_the_quoted_line_on_it(report_page):
    report_page.locator(".page-chip").first.wait_for(timeout=10_000)
    report_page.locator(".page-chip").first.click()
    box = report_page.locator(".lightbox")
    box.wait_for(state="visible", timeout=10_000)
    # The image is context; the quoted line is the evidence. Both have to be
    # there for "show me where this came from" to be answerable.
    assert box.locator("img").count() == 1
    assert box.locator(".lb-evidence").inner_text().strip() != ""


def test_the_evidence_page_is_shown_whole_not_cropped(report_page):
    """An element that exists is not an element that fits.

    `.lb-stage` used to size to its content, so `max-height: 100%` on the image
    resolved against a height that was itself the image's height: a 1,150x1,600
    fixture page rendered at full size inside a 960px overlay, overflowing it by
    320 top and bottom, and `overflow: hidden` cut the difference away. The
    reviewer asking "where did this number come from" got the middle of the page
    and lost the line they were chasing. The count-only assertion above passed
    the whole time, which is why this one exists.
    """
    report_page.locator(".page-chip").first.click()
    box = report_page.locator(".lightbox")
    box.wait_for(state="visible", timeout=10_000)
    report_page.wait_for_function(
        """() => { const i = document.querySelector('.lightbox img');
                   return i && i.complete && i.naturalWidth > 0; }""",
        timeout=15_000)
    fit = report_page.evaluate(
        """() => {
            const img = document.querySelector('.lightbox .lb-stage img');
            const r = img.getBoundingClientRect();
            return {fits: r.height <= innerHeight && r.width <= innerWidth
                        && r.top >= 0 && r.bottom <= innerHeight + 1,
                    img: [Math.round(r.height), Math.round(r.top), Math.round(r.bottom)],
                    viewport: innerHeight};
        }"""
    )
    assert fit["fits"], (
        f"the evidence image overflows the overlay: {fit['img']} in a "
        f"{fit['viewport']}px viewport, so the page is cropped on screen"
    )


def test_the_correction_dialog_edits_without_a_native_prompt(report_page, app):
    """window.prompt is unsupported in some embedded browsers and throws without
    a trace. The correction must be an in-page dialog: focusable, cancellable,
    validated, and recorded in the audit trail."""
    page = report_page
    sid = _shipment_id(page)
    row = page.locator("#cmp tbody tr").filter(has_text="Quantity").first
    row.locator(".edit-value").first.click()
    dialog = page.locator("#correction-dialog")
    dialog.wait_for(state="visible", timeout=10_000)
    assert dialog.locator("#correction-value").input_value() != ""

    # Empty input stays in the dialog and says why.
    page.fill("#correction-value", "")
    page.locator("#correction-save").click()
    assert page.locator("#correction-error").is_visible()
    assert dialog.is_visible()

    page.fill("#correction-value", "120 CARTONS")
    page.fill("#correction-actor", "e2e-ui")
    page.locator("#correction-save").click()
    dialog.wait_for(state="hidden", timeout=20_000)

    corrected = page.locator("#cmp td.corrected").first
    assert corrected.count() >= 1
    assert "corrected by e2e-ui" in corrected.inner_text()
    trail = _api(app, f"/api/shipments/{sid}/corrections")
    assert any(c["actor"] == "e2e-ui" and c["corrected_value"] == "120 CARTONS"
               for c in trail["corrections"])


def test_the_correction_dialog_cancels_without_changing_anything(report_page):
    page = report_page
    before = page.locator("#cmp").inner_text()
    page.locator(".edit-value").first.click()
    page.locator("#correction-cancel").click()
    assert page.locator("#correction-dialog").is_hidden()
    assert page.locator("#cmp").inner_text() == before


def test_a_correction_marks_the_row_and_survives_a_reload(report_page, app):
    """The reviewer flow, end to end: change a value through the API, reload, and
    the row must show it was corrected *and* what the model originally said."""
    sid = _shipment_id(report_page)
    assert sid, "the export link should name the shipment"
    report = _api(app, f"/api/shipments/{sid}/report")

    target = next(
        ({"doc": v["document_id"], "field": c["field"], "value": "999 CORRECTED"}
         for c in report["comparisons"] for v in c["values"] if v["document_id"]),
        None,
    )
    assert target, "no comparison carries a document to correct"

    _api(app, f"/api/shipments/{sid}/documents/{target['doc']}/fields/{target['field']}",
         payload={"value": target["value"], "actor": "e2e", "note": "browser test"})

    page = report_page
    page.goto(app.base_url + f"/?run={sid}")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_selector("#cmp tbody tr", timeout=60_000)

    marked = page.locator("#cmp td.corrected")
    assert marked.count() >= 1, "the corrected cell is not flagged in the table"
    text = marked.first.inner_text()
    assert "corrected by e2e" in text, text
    assert "999 CORRECTED" in text, text


# ---------------------------------------------------------------------------
# layout and access control
# ---------------------------------------------------------------------------


def test_a_history_failure_is_shown_not_rendered_as_empty(page, app):
    """A 500 used to be rendered as "No runs in this container yet": an outage
    that looked like emptiness, hiding every run the user had."""
    def fail_history(route):
        if route.request.method == "GET" and route.request.url.split("?")[0].endswith("/api/shipments"):
            route.fulfill(status=500, content_type="application/json",
                          body='{"detail": "simulated outage"}')
        else:
            route.continue_()

    page.route("**/api/shipments**", fail_history)
    page.goto(app.base_url + "/")
    message = page.locator("#history .alert")
    message.wait_for(timeout=10_000)
    assert "Could not load run history" in message.inner_text()
    assert "simulated outage" in message.inner_text()
    assert "No runs in this container yet" not in page.locator("#history").inner_text()


def test_the_table_scrolls_on_a_phone_instead_of_reflowing(app, new_page):
    """The mobile answer was a scrollable wrapper, not a reflow: five columns of
    evidence do not fit in 390px and must not be squeezed into something that no
    longer reads as a table."""
    page = new_page(viewport={"width": 390, "height": 844})
    page.goto(app.base_url + "/?demo=shipment_conflict")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_selector("#cmp tbody tr", timeout=60_000)

    wrap = page.locator("#cmp").locator(
        "xpath=ancestor::div[contains(@class,'table-wrap')]"
    )
    assert wrap.count() == 1, "the comparison table lost its scroll wrapper"
    assert wrap.evaluate("n => n.scrollWidth > n.clientWidth"), \
        "the wrapper should scroll horizontally at 390px"
    assert page.locator("#cmp tbody tr").count() >= FIELD_COUNT


def test_a_protected_instance_asks_for_a_key_and_the_key_opens_it(start_server, new_page):
    server = start_server(api_keys=KEY)
    assert server.json("/api/health")["api_protected"] is True

    page = new_page()
    page.goto(server.base_url + "/")
    page.wait_for_selector("#apikey-field:not([hidden])", timeout=20_000)
    assert "protected" in page.locator("#config-status").inner_text().lower()

    page.fill("#cfg-apikey", "the-wrong-key")
    page.click("#apikey-submit")
    page.wait_for_function(
        "() => document.querySelector('#config-status').textContent"
        ".includes('Could not unlock')", timeout=15_000)
    assert page.locator("#apikey-field").is_visible(), "a wrong key must not close the panel"

    page.fill("#cfg-apikey", KEY)
    page.click("#apikey-submit")
    page.wait_for_function(
        "() => document.querySelector('#fixture-list').children.length > 0",
        timeout=20_000)

    # The key is exchanged for a session and then dropped. If it survived in web
    # storage, any later script on this origin could read it back.
    leaked = page.evaluate(
        """() => [localStorage, sessionStorage]
             .flatMap(s => Object.entries(s))
             .filter(([, v]) => String(v).includes('e2e-secret-key'))
             .map(([k]) => k)"""
    )
    assert leaked == [], f"the API key persisted in web storage under {leaked}"

    # ...and the browser can now actually load a report through the session.
    page.goto(server.base_url + "/?demo=shipment_clean")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    assert page.locator("#cmp tbody tr").count() >= FIELD_COUNT


def test_a_protected_instance_refuses_the_data_routes_without_a_session(start_server):
    """The UI is only as protected as the API underneath it. /api/health stays
    open on purpose, so a monitor can still tell an outage from an auth wall."""
    server = start_server(api_keys=KEY)
    try:
        _api(server, "/api/shipments")
        raise AssertionError("the route answered without a session")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, exc.code
        body = exc.read().decode()
        assert KEY not in body, "a 401 must not echo anything about the key it refused"
    assert _api(server, "/api/health")["status"] == "ok"


# ---------------------------------------------------------------------------
# evidence verification
# ---------------------------------------------------------------------------


def test_no_unverified_card_appears_when_every_quote_was_confirmed(report_page, app):
    """The card is a warning, so it must be absent when there is nothing to warn
    about -- an always-visible empty 'Unverified evidence' panel would train
    people to ignore the one time it has content."""
    assert report_page.locator("#unverified-card").is_hidden()

    sid = _shipment_id(report_page)
    report = _api(app, f"/api/shipments/{sid}/report")
    checked = [v for d in report["documents"] for v in (d["verification"] or {}).values()]
    assert checked, "the fixture route should supply a text source, so checks ran"
    assert [v for v in checked if v["state"] == "unverified"] == []


def test_a_photo_upload_reports_its_quotes_as_not_checkable(new_page, start_server):
    """The honest-empty case for the app's most common real input.

    A degraded fixture image carries no text layer. The report must say the
    quotes could not be checked, and must not imply they passed.
    """
    server = start_server()
    page = new_page()
    page.goto(server.base_url + "/")
    page.wait_for_selector("#dropzone", timeout=20_000)

    upload = REPO / "fixtures" / "images" / "invoice_clean.png"
    page.set_input_files("#file-input", str(upload))
    page.wait_for_function(
        "() => !document.querySelector('#analyze').disabled", timeout=15_000)
    page.click("#analyze")
    page.wait_for_selector("#cmp tbody tr", timeout=180_000)

    sid = _shipment_id(page)
    report = _api(server, f"/api/shipments/{sid}/report")
    states = [v["state"] for d in report["documents"]
              for v in (d["verification"] or {}).values()]
    assert states, "verification should have run and recorded a state per field"
    assert set(states) <= {"not_checkable", "absent"}, set(states)
    assert report["unverified"] == [], "nothing may be excluded on an unchecked page"
    assert page.locator("#unverified-card").is_hidden()


# ---------------------------------------------------------------------------
# the status banner
# ---------------------------------------------------------------------------


def _settled(page, app, shipment: str):
    page.goto(app.base_url + f"/?demo={shipment}")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_function(
        """() => { const b = document.querySelector('#status-flag');
                   return b && b.textContent.trim().length > 0
                     && !/Still reconciling/i.test(b.textContent); }""",
        timeout=60_000)
    return page


def test_the_banner_sits_above_the_report_and_names_what_blocks_it(report_page):
    """One line an auditor can act on, at the top, with the items behind it
    printed there rather than left scattered through the panels below."""
    card = report_page.locator("#status-card")
    assert card.is_visible()
    assert "Blocked" in report_page.locator("#status-flag").inner_text()
    assert "container" in report_page.locator("#status-list li").first.inner_text().lower()
    assert report_page.locator("#status-list li.finding-critical").count() >= 1

    follows = report_page.evaluate(
        """() => (document.querySelector('#status-card')
                    .compareDocumentPosition(document.querySelector('.verdict')) & 4) !== 0""")
    assert follows, "the banner has to be read before the detail, not after it"

    # The two numbers it must never contradict are printed beside it.
    note = report_page.locator("#status-note").inner_text()
    assert "verdict" in note.lower() and "risk" in note.lower()


def test_the_clean_fixture_is_called_ready_and_says_what_was_never_checked(new_page, app):
    """"Ready" is a claim about blockers, not a guarantee that everything was
    cross-checked -- and the page is expected to say so out loud."""
    page = _settled(new_page(), app, "shipment_clean")
    try:
        assert "is-ready" in (page.locator("#status-card").get_attribute("class") or "")
        assert "Ready to ship" in page.locator("#status-flag").inner_text()
        assert page.locator("#status-list li").count() == 0
        assert "not that everything was checked" in \
            page.locator("#status-note").inner_text()
    finally:
        page.close()


def test_the_banner_never_contradicts_the_verdict_it_prints_beside(new_page, app):
    """Checked through the HTTP report the browser was handed, for every bundled
    fixture: the dangerous direction is a ready banner over a verdict that
    withheld it, and that is the one combination the server must never emit."""
    for shipment in ("shipment_clean", "shipment_conflict", "shipment_messy"):
        page = _settled(new_page(), app, shipment)
        try:
            sid = _shipment_id(page)
            report = _api(app, f"/api/shipments/{sid}/report")
            banner = report["status_report"]["banner"]
            verdict = report["summary"]["verdict"]
            risk = report["risk"]["score"]
            assert banner in ("blocked", "review", "ready"), shipment
            if verdict in ("conflicts", "review"):
                assert banner != "ready", f"{shipment}: {verdict} with {banner}"
            if risk >= report["risk"]["medium_at"]:
                assert banner != "ready", f"{shipment}: risk {risk} with {banner}"
            assert banner in page.locator("#status-flag").inner_text().lower(), shipment
        finally:
            page.close()


# ---------------------------------------------------------------------------
# highlighting the region a value came from
# ---------------------------------------------------------------------------


def _open_chip(page, index: int):
    page.locator(".page-chip").nth(index).click()
    page.locator(".lightbox").wait_for(state="visible", timeout=10_000)


def test_a_page_with_a_text_layer_can_be_pointed_at(report_page):
    """The claim this feature exists to support: not "this value was checked" but
    "it was checked here, on this part of the page".

    Walked over every chip rather than picking one, because a chip whose quote
    joined two table cells honestly has no rectangle, and a test that only looked
    at the first chip would pass on whichever the fixture happened to put there.
    """
    chips = report_page.locator(".page-chip")
    total = chips.count()
    assert total >= 4, f"only {total} evidence chips rendered"

    placed = 0
    for i in range(total):
        _open_chip(report_page, i)
        box = report_page.locator(".lb-box")
        note = report_page.locator(".lb-nocoords")
        if box.count() == 1:
            placed += 1
            style = box.get_attribute("style") or ""
            for prop in ("left:", "top:", "width:", "height:"):
                assert prop in style, style
            assert "%" in style
            assert note.count() == 0, "a page that was located must not also apologise"
        else:
            assert box.count() == 0
            assert note.count() == 1, "no rectangle and no explanation of why"
            assert "No coordinates available" in note.inner_text()
        report_page.keyboard.press("Escape")
        assert report_page.locator(".lightbox").count() == 0, "Esc did not close it"

    assert placed >= 1, "not one chip could point at its line, though the pages have text"


def test_a_photo_shows_the_page_and_says_it_has_no_coordinates(new_page, start_server):
    """The fallback, in the browser rather than in a docstring.

    A degraded fixture image carries no text layer, so there is nothing to draw
    from. The page still opens with the quoted line -- and says out loud that the
    region is not marked, instead of leaving the reader to infer it from a
    rectangle that isn't there.
    """
    server = start_server()
    page = new_page()
    page.goto(server.base_url + "/")
    page.wait_for_selector("#dropzone", timeout=20_000)
    page.set_input_files("#file-input",
                         str(REPO / "fixtures" / "images" / "invoice_clean.png"))
    page.wait_for_function(
        "() => !document.querySelector('#analyze').disabled", timeout=15_000)
    page.click("#analyze")
    page.wait_for_selector("#cmp tbody tr", timeout=180_000)

    chip = page.locator(".page-chip")
    assert chip.count() >= 1, "no evidence chip reached the table"
    _open_chip(page, 0)
    assert page.locator(".lb-box").count() == 0
    note = page.locator(".lb-nocoords")
    assert note.count() == 1
    assert "No coordinates available" in note.inner_text()
    assert page.locator(".lb-evidence").inner_text().strip() != ""
    page.close()


# ---------------------------------------------------------------------------
# correction drafts
# ---------------------------------------------------------------------------


def test_the_drafts_a_report_writes_are_copyable_and_unedited(report_page, app):
    """The panel exists so a reviewer does not retype a discrepancy into an email,
    which is how a wrong number leaves the building. Two things have to be true in
    the browser: the text shown is the server's text, and the button hands over
    that same text rather than something assembled here."""
    card = report_page.locator("#emails-card")
    card.wait_for(state="visible", timeout=15_000)
    drafts = card.locator(".email")
    assert drafts.count() >= 1

    sid = _shipment_id(report_page)
    mails = _api(app, f"/api/shipments/{sid}/report")["correction_emails"]
    assert len(mails) == drafts.count()

    for block in range(drafts.count()):
        shown = drafts.nth(block)
        label = shown.locator(".email-meta").inner_text().split(" · ")[0]
        mail = next(m for m in mails if m["document_label"] == label)
        # Byte-for-byte: a draft re-composed client-side is a draft that can say
        # something the findings did not.
        assert shown.locator(".email-body").inner_text() == mail["body"]
        assert shown.locator(".email-to").inner_text().startswith("To ")
        assert "no language model was asked" in mail["body"]
        assert "No contact address is on file" in mail["body"]
        assert shown.locator("button").last.inner_text() == "Copy this draft"


def test_no_drafts_panel_appears_when_there_is_nothing_to_write(new_page, app):
    """The panel is a request to somebody. On a clean shipment nobody owes an
    answer, and a card that said so would be noise that trains people to skip it."""
    page = new_page()
    page.goto(app.base_url + "/?demo=shipment_clean")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    page.wait_for_function(
        "() => { const b = document.querySelector('#status-flag');"
        " return b && /Ready to ship/i.test(b.textContent); }", timeout=60_000)
    assert page.locator("#emails-card").is_hidden()
    page.close()
