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
