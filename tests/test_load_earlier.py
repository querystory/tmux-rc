"""Earlier history for the live view: rows addressed by absolute index from the oldest kept
row, so chunks fetched before new output still line up after it; the /history endpoint's
bounds; and chunks rendering exactly as the same rows would in one capture.

The real-tmux test runs on a private server socket, never the user's."""

import json
import shutil
import subprocess
import time
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from openbus import server, tmux

TERMINAL_JS = (Path(__file__).resolve().parent.parent / "web" / "terminal.js").as_uri()
# Colors left open across rows (31 at row 7k, reset at 7k+3) and a link every 3 rows, long
# enough to wrap at 80 columns: chunk boundaries land inside colored runs and wrapped links.
PRINT = ("awk 'BEGIN{{for(i={0};i<{0}+{1};i++){{if(i%7==0)printf \"\\033[31m\";"
         'if(i%7==3)printf "\\033[0m";'
         'printf "L%05d%s\\n",i,(i%3?"":" https://example.com/" i "/' + "a" * 120 + " end\")}}}}'")


@pytest.fixture
def pane(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    socket = str(tmp_path / "earlier.sock")

    def run(args):
        return subprocess.check_output(["tmux", "-S", socket, *args], text=True)

    monkeypatch.setattr(tmux, "_run", run)
    pane = run(["-f", "/dev/null", "start-server", ";", "set", "-g", "history-limit", "50000",
                ";", "new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "80", "-y", "10",
                "sh"]).strip()

    def emit(start, count):  # numbered rows L<start>.., then wait until tmux has them all
        run(["send-keys", "-t", pane, PRINT.format(start, count), "Enter"])
        for _ in range(250):
            if f"L{start + count - 1:05d}" in run(["capture-pane", "-p", "-t", pane]):
                return
            time.sleep(0.02)
        pytest.fail("tmux never received the rows")

    try:
        yield pane, emit, run
    finally:
        window = run(["display-message", "-p", "-t", pane, "#{window_id}"]).strip()
        subprocess.run(["tmux", "-S", socket, "kill-window", "-t", window], check=False)


def numbers(text):
    return [int(word[1:6]) for word in tmux._ANSI.sub("", text).split()
            if word.startswith("L") and word[1:6].isdigit()]


def test_chunks_stay_aligned_while_history_grows(pane):
    pane, emit, _ = pane
    emit(1, 3000)
    frame, top, screen = tmux.capture_live(pane)
    assert screen == "80" and top > 2500
    first = numbers(frame)[0]
    # Output arrives between the frame and every chunk; absolute rows don't move.
    chunks, before = [], top
    while before:
        emit(10_000 + 1000 * len(chunks), 400)
        text, before, _ = tmux.capture_history(pane, before, 500)
        chunks.insert(0, text)
    held = numbers("\n".join(chunks))
    assert held[0] == 1 and held[-1] == first - 1
    assert [n for n in held if n < 10_000] == list(range(1, first))
    # The gap: rows the frame moved past since `top` are exactly the ones between.
    _, new_top, _ = tmux.capture_live(pane)
    gap, start, _ = tmux.capture_history(pane, new_top, new_top - top)
    later = tmux.capture_history(pane, new_top + 1, 1)[0]
    assert start == top and numbers(gap)[0] == first
    assert numbers(gap + "\n" + later) == sorted(numbers(gap + "\n" + later))


def test_chunks_render_like_one_capture(pane):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    pane, emit, run = pane
    emit(1, 1200)
    _, top, _ = tmux.capture_live(pane)
    chunks, before = [], top
    while before:
        text, before, wraps = tmux.capture_history(pane, before, 333)
        chunks.insert(0, (text, wraps))
    size = int(run(["display-message", "-p", "-t", pane, "#{history_size}"]))
    whole = tmux._materialize_links(run(  # the same rows, in one plain capture
        ["capture-pane", "-p", "-J", "-e", "-t", pane, "-S", "-", "-E", str(top - 1 - size)]))
    whole = whole.removesuffix("\n")
    # Chunks join where tmux wrapped a line across their edge, as the client rejoins them.
    assert any(wraps for _, wraps in chunks[:-1])
    groups = [""]
    for text, wraps in chunks:
        groups[-1] += text
        groups.append("") if not wraps else None
    groups = [g for g in groups if g]
    # Same rows and lines; only the SGR differs, as each capture restates the open color.
    assert tmux._ANSI.sub("", "\n".join(groups)) == tmux._ANSI.sub("", whole)
    script = (f"import {{ renderCaptureLines }} from {json.dumps(TERMINAL_JS)};"
              "import { readFileSync } from 'node:fs';"
              "const { whole, groups } = JSON.parse(readFileSync(0, 'utf8'));"
              "const one = renderCaptureLines(whole, { color: true });"
              "const split = groups.flatMap((c) => renderCaptureLines(c, { color: true }));"
              "process.stdout.write(JSON.stringify({ one, split }));")
    out = json.loads(subprocess.check_output(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps({"whole": whole, "groups": groups}), text=True))
    assert out["split"] == out["one"]
    assert "color:" in "".join(out["one"]) and "<a href" in "".join(out["one"])


def test_rejoined_edges_render_like_one_capture(pane):
    """The client's own rejoin at one edge (app.js loadEarlier): a gap's first piece after
    the held last line, or a prepended chunk's last piece before the held first line. Each
    must keep the colors the line had, which the held piece alone does not carry."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    pane, emit, run = pane
    emit(1, 600)
    _, top, _ = tmux.capture_live(pane)
    chunks, before = [], top
    while before:
        text, start, wraps = tmux.capture_history(pane, before, 40)
        chunks.insert(0, (text, start, before, wraps))
        before = start
    size = int(run(["display-message", "-p", "-t", pane, "#{history_size}"]))
    pairs = [{"a": a[0], "b": b[0], "whole": tmux._materialize_links(run(
        ["capture-pane", "-p", "-J", "-e", "-t", pane, "-S", str(a[1] - size),
         "-E", str(b[2] - 1 - size)])).removesuffix("\n")}
        for a, b in pairwise(chunks) if a[3]]
    assert any("\x1b[31m" in p["a"].rsplit("\n", 1)[0] for p in pairs)  # a colored head
    names = "renderCaptureLines as render, lastLine, joinWrapped"
    script = (f"import {{ {names} }} from {json.dumps(TERMINAL_JS)};"
              "import { readFileSync } from 'node:fs';"
              "const c = { color: true }, bad = [];"
              "const r = (t) => render(t, c).map((l) => l.replace(/<span[^>]*><\\/span>/g, ''));"
              "for (const { a, b, whole } of JSON.parse(readFileSync(0, 'utf8'))) {"
              "  const one = JSON.stringify(r(whole));"
              "  const gap = [...r(a).slice(0, -1), ...r(joinWrapped(lastLine(a), b))];"
              "  const up = [...r(joinWrapped(a, b.split('\\n')[0])), ...r(b).slice(1)];"
              "  if (JSON.stringify(gap) !== one || JSON.stringify(up) !== one) bad.push(whole);"
              "}"
              "process.stdout.write(JSON.stringify(bad));")
    bad = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", script],
                                             input=json.dumps(pairs), text=True))
    assert pairs and bad == []


def test_endpoint_bounds_and_pane_id(monkeypatch):
    calls = []
    def run(args):
        calls.append(args)
        nonce = args[-1].split("display-message -p ")[-1].split()[0]  # the edge probe's
        return f"5 80 0\nrow\n{nonce}\nrow\nnext\n"

    monkeypatch.setattr(tmux, "_run", run)
    client = TestClient(server.app)
    url = "/api/panes/{}/history"
    answer = client.get(url.format("%251"), params={"before": 3}).json()
    assert answer == {"text": "row", "start": 0, "wraps": False}
    assert "-S #{e|-|:0,#{history_size}} -E #{e|-|:2,#{history_size}} ;" in calls[0][-1]
    # Capped per request, never negative, and `before` is required.
    for params in ({"before": 9, "lines": 1001}, {"before": 9, "lines": 0}, {"before": -1}, {}):
        assert client.get(url.format("%251"), params=params).status_code == 422
    # Nothing above the oldest row: answered without asking tmux.
    calls.clear()
    assert client.get(url.format("%251"), params={"before": 0}).json() == {
        "text": "", "start": 0, "wraps": False}
    # The id is spliced into a tmux command string: anything but %N is a missing pane.
    for bad in ("main", "%1 ; kill-server", "%1;x"):
        assert client.get(url.format(quote(bad, safe="")), params={"before": 9}).status_code == 404
    assert calls == []


@pytest.mark.parametrize(("alt", "top", "screen"), [("0", 300, "80"), ("1", 0, "80a")])
def test_alternate_screen_has_nothing_earlier(monkeypatch, alt, top, screen):
    # A fullscreen app's history is its own (reached by the wheel), not tmux's rows above.
    monkeypatch.setattr(tmux, "_run", lambda args: f"500 80 {alt}\nscreen\n\n")
    assert tmux.capture_live("%1") == ("screen", top, screen)  # a switch renumbers rows
