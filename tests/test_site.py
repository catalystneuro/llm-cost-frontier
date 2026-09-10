"""End-to-end smoke tests for the dashboard.

The site is assembled the way the deploy workflow assembles it and served
locally; a headless browser then walks the tabs, the era toggle, and two
viewports, asserting the invariants that have historically broken: clean
console, charts that render and switch, no horizontal overflow on a phone,
and no width ratchet after rotation.

Requires playwright (pip install playwright; playwright install chromium);
skipped when it is not installed, so the unit suite stays dependency-free.
"""
import functools
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (REPO / "build/llm-frontier.json").exists(), reason="no built data")


@pytest.fixture(scope="module")
def site_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    shutil.copytree(REPO / "site", root, dirs_exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    shutil.copy(REPO / "build/llm-frontier.json", root / "data/llm-frontier.json")
    shutil.copy(REPO / "build/feed.xml", root / "feed.xml")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def open_page(browser, site_url, width=1300, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.goto(site_url + "/")
    page.wait_for_selector("#pfc-frontier svg")
    page.wait_for_selector("#pfc-records svg")
    return ctx, page, errors


def test_loads_clean_and_renders(browser, site_url):
    ctx, page, errors = open_page(browser, site_url)
    try:
        assert errors == []
        assert page.locator("#pfc-frontier svg").count() == 1
        assert page.locator("#pfc-records svg").count() == 1
        # The finished frontier is labeled today and the era toggle is present.
        assert "today" in page.locator("#pfc-frontier-stage").inner_text()
        assert page.locator("#pfc-era-nav button").count() >= 2
        # Advances listed; the staleness note stays hidden for a fresh file.
        assert page.locator("#pfc-advances .pfc-adv-day").count() > 0
        assert page.locator("#pfc-stale").is_hidden()
    finally:
        ctx.close()


def test_capability_tab_switches_everything(browser, site_url):
    ctx, page, errors = open_page(browser, site_url)
    try:
        page.locator(".pfc-tab", has_text="Coding").click()
        page.wait_for_selector("#pfc-frontier svg text:text('Terminal-Bench 2.1')")
        assert page.evaluate("location.hash") == "#coding"
        assert page.locator("#pfc-cap-table tr").count() > 0
        # The records chart follows the tab too.
        assert page.locator("#pfc-records-lead").inner_text().startswith("The cheapest cost per task")
        assert "Terminal-Bench" in page.locator("#pfc-records-lead").inner_text()
        assert errors == []
    finally:
        ctx.close()


def test_era_archive_view(browser, site_url):
    ctx, page, errors = open_page(browser, site_url)
    try:
        page.locator("#pfc-era-nav button").first.click()
        page.wait_for_selector("#pfc-records svg text:text('measurements ended')")
        assert "Sep 4, 2026" in page.locator("#pfc-frontier-stage").inner_text()
        # Back to the current era.
        page.locator("#pfc-era-nav button", has_text="current").click()
        page.wait_for_selector("#pfc-frontier-stage:text('today')")
        assert errors == []
    finally:
        ctx.close()


def test_phone_never_overflows_even_after_rotation(browser, site_url):
    ctx, page, errors = open_page(browser, site_url, width=390, height=844)
    try:
        def overflow():
            return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow() <= 2
        # Rotate to landscape and back: the grid track must not ratchet.
        page.set_viewport_size({"width": 844, "height": 390})
        page.wait_for_timeout(400)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        assert overflow() <= 2
        # Date ticks stay sparse enough to read at this width.
        ticks = page.locator("#pfc-records svg text", has_text="'2").count()
        assert 2 <= ticks <= 5
        assert errors == []
    finally:
        ctx.close()
