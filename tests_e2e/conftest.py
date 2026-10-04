"""Live-browser tests for the UI, against a real server on a real port.

WHY THIS DIRECTORY IS SEPARATE FROM tests/
    `tests/` guarantees zero network calls and enforces that by patching the
    socket layer for its whole session (tests/conftest.py). These tests need the
    opposite: a browser process talking HTTP to a uvicorn process. They cannot
    share a session, so they do not share a directory, and `testpaths = tests` in
    pytest.ini keeps a bare `pytest` from ever collecting this file.

    Run them on their own: `make e2e`, or `python -m pytest tests_e2e -q`.
    Running `pytest tests tests_e2e` together would let the socket guard refuse
    the loopback connection, because the guard only permits ports the test
    process itself bound and the server binds this one in another process. The
    `no_socket_guard` fixture below turns that confusing failure into a clear one.

WHAT THESE PROVE THAT THE PYTHON TESTS CANNOT
    That the DOM the browser actually builds shows the right thing: that a panel
    is hidden rather than empty, that provenance text reads honestly on a demo
    run, that a table scrolls on a phone instead of reflowing, and that the
    unlock flow survives a 401. `textContent`-only construction is a convention
    until something renders it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every one of these is overridden by start_server(**env); the point is that no
# key on this machine can leak into a run, and a demo run is reproducible
# wherever it happens.
BASE_ENV = {
    "DEMO_MODE": "true",
    "VISION_PROVIDER": "nvidia",
    "NVIDIA_API_KEY": "",
    "NEBIUS_API_KEY": "",
    "ENABLE_SUMMARY": "false",
    "ENABLE_ADJUDICATION": "false",
    "ENABLE_BRIEFING": "false",
    "MODEL_DISCOVERY": "false",
    "API_KEYS": "",
    "CONFIG_TOKEN": "",
    "ENABLE_LINE_ITEMS": "false",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session", autouse=True)
def no_socket_guard():
    """Fail loudly if the offline guard is somehow active for this session."""
    if socket.socket.connect.__name__ != "connect":
        pytest.skip(
            "the tests/ offline socket guard is active in this session; run the "
            "browser suite on its own with `make e2e`"
        )


class Server:
    """A running app, plus the two ways a test needs to reach it."""

    def __init__(self, proc: subprocess.Popen, base_url: str, root: Path, log):
        self.proc = proc
        self.base_url = base_url
        self.root = root
        self.log_path = root / "server.log"
        self._log = log

    def json(self, path: str, timeout: float = 20.0):
        import json

        with urllib.request.urlopen(self.base_url + path, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def tail_log(self) -> str:
        self._log.flush()
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return "(log unreadable)"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
        try:
            self._log.close()
        except OSError:  # pragma: no cover
            pass


@pytest.fixture(scope="session")
def start_server(tmp_path_factory):
    """Launch the app the way a user would, and tear every instance down after."""
    created: list[Server] = []

    def _start(**env) -> Server:
        port = _free_port()
        root = tmp_path_factory.mktemp(f"e2e-{port}")
        child_env = {
            **os.environ,
            "PYTHONPATH": str(REPO),
            **BASE_ENV,
            "CACHE_DIR": str(REPO / "fixtures" / "responses"),
            "RUNS_DIR": str(root),
            **{str(k).upper(): str(v) for k, v in env.items()},
        }
        # The handle has to stay open for the child's whole life, so this cannot
        # be a `with` block; Server.close() below is what releases it.
        log = open(root / "server.log", "wb")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(REPO), env=child_env, stdout=log, stderr=log,
        )
        server = Server(proc, f"http://127.0.0.1:{port}", root, log)
        created.append(server)

        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"server exited early (code {proc.returncode}); "
                    f"log:\n{server.tail_log()}"
                )
            try:
                with urllib.request.urlopen(server.base_url + "/api/health", timeout=2):
                    return server
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        proc.terminate()
        pytest.fail(f"server on port {port} never became healthy; log:\n{server.tail_log()}")

    yield _start

    for server in created:
        server.stop()


@pytest.fixture(scope="session")
def app(start_server) -> Server:
    return start_server()


@pytest.fixture(scope="session")
def browser_session():
    """One chromium for the session.

    pytest-playwright's own `browser` fixture would do, but it is bound to that
    plugin's per-test context machinery and this suite needs contexts at chosen
    viewports and with cookies cleared between tests.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def new_page(browser_session, app):
    """A factory: `new_page()` for the default desktop viewport, or a size."""
    opened = []

    def _make(viewport: dict | None = None):
        context = browser_session.new_context(
            viewport=viewport or {"width": 1280, "height": 900}
        )
        page = context.new_page()
        page.errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.base_url = app.base_url  # type: ignore[attr-defined]
        opened.append(context)
        return page

    yield _make

    for context in opened:
        context.close()


@pytest.fixture
def page(new_page):
    return new_page()


@pytest.fixture
def report_page(new_page, app):
    """A page already showing the finished "conflict" fixture shipment.

    Most of these tests care about a settled report, and the demo cache makes
    that take a second rather than a model call.
    """
    page = new_page()
    page.goto(app.base_url + "/?demo=shipment_conflict")
    page.wait_for_selector("#results:not([hidden])", timeout=60_000)
    # Settled means the headline is one of the three verdicts, not the running
    # or working placeholder. Anything looser races the poll.
    page.wait_for_function(
        """() => ['All cross-checked fields agree',
                  'No hard conflicts — a few fields deserve a glance',
                  'Conflicts found — reconcile before the goods move']
             .includes(document.querySelector('#verdict-headline').textContent.trim())""",
        timeout=60_000,
    )
    return page
