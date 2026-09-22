"""Mobile URLs work behind a TLS-terminating tunnel, with either slash form."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openbus import server

app = server.app


@pytest.mark.parametrize("path", ["/m", "/m/"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_mobile_entrypoint(path, method):
    client = TestClient(app)
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" not in response.headers.get("content-disposition", "")
    if method == "GET":
        assert 'src="/m/app.js"' in response.text
        assert 'href="/m/style.css"' in response.text
    assert "no-store" in response.headers["cache-control"]


def test_mobile_assets_and_manifest():
    client = TestClient(app)
    for path in (
        "/m/app.js",
        "/m/style.css",
        "/m/live.js",
        "/m/composer.js",
        "/m/pane-model.js",
        "/lm-tap.js",
        "/terminal.js",
        "/icon.svg",
    ):
        assert client.get(path).status_code == 200
    manifest = client.get("/m/manifest.json").json()
    assert manifest["start_url"] == "/m"
    assert manifest["scope"] == "/m"
    assert 'src="/app.js"' in client.get("/").text


def test_version_tracks_nested_mobile_assets(tmp_path, monkeypatch):
    (tmp_path / "app.js").write_text("desktop")
    mobile = tmp_path / "m"
    mobile.mkdir()
    asset = mobile / "app.js"
    asset.write_text("mobile")
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    before = server.get_version()["version"]
    stat = asset.stat()
    os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    edited = server.get_version()["version"]
    assert edited != before
    mobile.rename(tmp_path / "renamed")
    assert server.get_version()["version"] != edited


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("directory_exists", [False, True])
def test_mobile_missing_assets_return_html_404(
    tmp_path, monkeypatch, method, directory_exists
):
    web = tmp_path / "web"
    if directory_exists:
        (web / "m").mkdir(parents=True)
    monkeypatch.setattr(server, "WEB_DIR", web)
    response = TestClient(app).request(method, "/m")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" not in response.headers.get("content-disposition", "")
    if method == "GET":
        assert "Mobile UI assets are not installed" in response.text


def test_key_row_can_answer_a_codex_queued_question():
    """Codex parks follow-up questions behind "shift + ← to answer". Without S-Left in
    the key row there is NO way to reach that from a phone — the question is visible and
    unanswerable. tmux sends S-Left as CSI 1;2D, the sequence codex reads, so this needs
    no new machinery: it is a key NAME, sent literal=false like Esc and Ctrl-C."""
    app = (Path(__file__).resolve().parents[1] / "web/m/app.js").read_text()
    lines = app.splitlines()
    start = next(i for i, ln in enumerate(lines) if '"Esc", "Escape"' in ln)
    assert '"S-Left"' in lines[start], "shift+left missing from the mobile key row"
    # The dispatch these buttons SHARE must send a key NAME: literal would type the
    # characters "S-Left" into the pane. Assert it on the loop body that builds them,
    # not anywhere in the file — a future special-case handler for this entry has to
    # break this test rather than slip past a substring match elsewhere.
    body = "\n".join(lines[start:start + 12])
    assert "literal: false" in body, "key-row buttons must send key names, not literal text"
    assert "enter: false" in body, "a key-row press must not append a Return"
    # The label is a glyph, so the SPOKEN name has to be carried separately: a screen
    # reader handed "⇧←" announces two arrow characters or nothing at all, which
    # is useless for the one key a non-visual user opened this row to press.
    assert '"Shift+Left"' in lines[start], "S-Left needs a spoken label beside its glyph"
    assert 'aria-label", aria' in body, "the spoken label must be what reaches aria-label"


def test_wide_screens_keep_the_list_and_the_pane_on_screen_together():
    """The phone layout is list-OR-pane; a desktop has room for both. The whole switch is
    one media query plus not hiding the list — if a second render path ever appears here,
    the two-UI divergence this direction exists to end has just been recreated inside one
    UI. So pin the shape: a `wide` read gating the list's visibility, and a re-render when
    the breakpoint is crossed (without which widening leaves the sidebar hidden until the
    next poll repaints)."""
    root = Path(__file__).resolve().parents[1]
    app = (root / "web/m/app.js").read_text()
    css = (root / "web/m/style.css").read_text()

    assert "matchMedia(" in app and "WIDE" in app
    assert 'show("sessions", !inPane || wide)' in app, "the list must survive on wide screens"
    assert 'show("back", inPane && !wide)' in app, "Back has no sidebar to return to"
    # Feature-detected: older iOS Safari has only the deprecated addListener, and calling
    # the modern name unguarded throws at module scope, taking the whole phone UI with it.
    assert 'WIDE.addEventListener("change", resizeWorkspace)' in app, "crossing it must re-render"
    assert "WIDE.addListener(resizeWorkspace)" in app, "older iOS Safari must also re-render"

    # The CSS breakpoint and the JS one are the same number in two files; a mismatch would
    # show as a sidebar that is hidden in a grid column reserved for it.
    assert "min-width: 1100px" in css and "min-width: 1100px)" in app
    assert "grid-template-columns" in css


def test_wide_screens_fill_the_main_column_and_let_the_seam_move():
    """Three properties of the wide layout that are easy to regress silently.

    The main column must never be blank: with no pane picked it answers what the sidebar
    cannot, which is what the whole fleet is doing. Its counts come from the same helpers
    the filter tabs use, because a second count that disagreed with the tabs would be
    worse than showing none. The sidebar width is a clamped CSS variable, so a value
    stored from a wider window can still never squeeze out the list or the terminal. And
    the pane's identity lives over the pane: the header spans both columns here, so a
    title there would float above the sidebar it does not describe.
    """
    root = Path(__file__).resolve().parents[1]
    app = (root / "web/m/app.js").read_text()
    css = (root / "web/m/style.css").read_text()
    html = (root / "web/m/index.html").read_text()

    assert 'show("landing", wide && !inPane)' in app, "no pane picked must not mean a blank column"
    assert "renderLanding()" in app
    # The same helpers the tabs count with — not a parallel definition that can drift.
    assert "panes.filter(needsYou)" in app and "panes.filter(isRunning)" in app

    # The drag handle writes a variable the grid clamps; the clamp is the real guard.
    assert "--sidebar" in css and "clamp(260px" in css
    assert "setSidebar" in app and "pointermove" in app
    assert "ArrowLeft" in app, "a pointer-only resize is unreachable from the keyboard"

    # The heading moved out of the app-wide header and into the panel it names.
    assert html.index('id="detail"') < html.index('id="heading"'), "the title belongs over the pane"
