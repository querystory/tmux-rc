"""renderCaptureLines splits a rendered capture into ONE HTML string per screen line, so
the terminal paint can diff a streamed frame line-by-line instead of swapping the whole
subtree (web/app.js paintTerm, and THE RENDER INVARIANT at the top of that file).

The split is not String.split("\\n"): renderCapture emits spans and anchors that STRADDLE
newlines (a color run over three rows is one <span>; a URL a TUI hard-wrapped is one <a>
whose shown text keeps the breaks), so cutting on a newline naively would hand each line
unbalanced markup. The walker closes the open-tag stack at each newline and reopens it on
the next line.

These are the invariants a future edit to either function must not break:
  * the visible TEXT is byte-identical to the single-string renderCapture output,
  * one line out per newline in that output (so line N of the array is screen line N),
  * every line is self-contained balanced markup containing no raw newline,
  * no line invents a style/href the whole-capture render did not emit,
  * a wrap-joined URL keeps ONE identical href on each visual fragment.

Driven through `node` against the real web/terminal.js — no npm dependency and no second
copy of the renderer to drift (the repo has no JS build step; node is already required to
syntax-check the modules).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TERMINAL_JS = Path(__file__).resolve().parent.parent / "web" / "terminal.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to exercise web/terminal.js"
)

# Each case pairs a name with a capture. ESC is written as \x1b so the file stays
# ASCII-clean (embedding raw control bytes makes tools read the file as binary).
CASES = {
    "color_straddles_lines": "\x1b[32mgreen one\ngreen two\ngreen three\x1b[0m\nplain",
    "nested_sgr_straddles": "\x1b[1;4;31mbold under red\nsecond\x1b[0m\ntail",
    "bg_and_256_color": "\x1b[48;5;22m\x1b[38;5;231mhi\nthere\x1b[0m",
    "truecolor": "\x1b[38;2;10;20;30mrgb\nrun\x1b[0m",
    "markdown_link": "see [PR #7](https://x.io/pr/7) done\nnext line",
    "needs_escaping": "<script>&\"'\nmore <b>x</b>",
    "box_drawing": "╭────╮\n│ x │\n╰────╯",
    "blank_lines": "a\n\n\nb",
    "trailing_newline": "a\nb\n",
    "single_line": "just one line",
    "empty": "",
    "malformed_escapes": "\x1b[38;5;999mx\n\x1b[38;9my\n\x1b[z",
    # A URL the TUI hard-wrapped across two full-width lines: linkify's cross-line join
    # is a GRID heuristic reading every line's length, which is exactly why the parse has
    # to stay whole-capture and only the RESULT gets split.
    "wrapped_url": (
        "a" * 42 + " https://ex.com/very/long/pa\n"
        "th/continues/here/" + "x" * 41 + "\nshort\n" + "z" * 71
    ),
}

# Runs in node: renders each capture BOTH ways and reports the facts to assert on.
_PROBE = r"""
const { renderCapture, renderCaptureLines } = await import(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
const strip = (h) => h.replace(/<[^>]*>/g, "");
// Balanced AND well-nested? Our own output is generated, so a plain stack check is exact.
function balanced(h) {
  const st = [];
  for (const m of h.matchAll(/<(\/?)([a-z]+)[^>]*>/gi)) {
    if (m[1]) { if (st.pop() !== m[2].toLowerCase()) return false; }
    else st.push(m[2].toLowerCase());
  }
  return st.length === 0;
}
const attrs = (h) => (h.match(/(?:style|href)="[^"]*"/g) || []).sort();
const out = {};
for (const [name, text] of Object.entries(cases)) {
  out[name] = {};
  for (const color of [true, false]) {
    const whole = renderCapture(text, { color });
    const lines = renderCaptureLines(text, { color });
    const wholeAttrs = new Set(attrs(whole));
    out[name][color ? "color" : "plain"] = {
      wholeText: strip(whole),
      joinedText: lines.map(strip).join("\n"),
      lineCount: lines.length,
      wantLineCount: whole.split("\n").length,
      allBalanced: lines.every(balanced),
      noRawNewline: lines.every((l) => !l.includes("\n")),
      noNewAttrs: lines.every((l) => attrs(l).every((a) => wholeAttrs.has(a))),
      hrefs: lines.flatMap((l) => [...l.matchAll(/href="([^"]*)"/g)].map((m) => m[1])),
    };
  }
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Render every case through the real module once; assert per-property below."""
    probe = tmp_path_factory.mktemp("probe") / "probe.mjs"
    probe.write_text(_PROBE)
    res = subprocess.run(
        ["node", str(probe), TERMINAL_JS.as_uri(), json.dumps(CASES)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the assert below reports node's stderr on failure
    )
    assert res.returncode == 0, f"probe failed:\n{res.stderr}"
    return json.loads(res.stdout)


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("mode", ["color", "plain"])
def test_visible_text_is_identical_to_the_whole_render(rendered, name, mode):
    """The split may only move markup, never change a single visible character."""
    r = rendered[name][mode]
    assert r["joinedText"] == r["wholeText"]


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("mode", ["color", "plain"])
def test_one_line_out_per_screen_line(rendered, name, mode):
    """Line N of the array must BE screen line N — paintTerm diffs by index."""
    r = rendered[name][mode]
    assert r["lineCount"] == r["wantLineCount"]


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("mode", ["color", "plain"])
def test_every_line_is_self_contained_markup(rendered, name, mode):
    """Each line is assigned to its own node's innerHTML, so each must stand alone."""
    r = rendered[name][mode]
    assert r["allBalanced"], "a line has unbalanced/mis-nested tags"
    assert r["noRawNewline"], "a line kept a raw newline (the break is structural now)"


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("mode", ["color", "plain"])
def test_no_line_invents_styling(rendered, name, mode):
    """Reopened tags must be verbatim: no style or href the whole render didn't emit."""
    assert rendered[name][mode]["noNewAttrs"]


def test_wrapped_url_anchors_every_fragment_to_the_same_href(rendered):
    """A URL split across wrapped lines becomes one anchor PER VISUAL FRAGMENT, each
    carrying the whole href — the clickability contract linkify already documents. The
    join itself is the whole-capture parse's job; this pins that splitting kept it."""
    hrefs = rendered["wrapped_url"]["plain"]["hrefs"]
    assert len(hrefs) == 2, f"expected one anchor per wrapped fragment, got {hrefs}"
    assert hrefs[0] == hrefs[1]
    assert hrefs[0] == "https://ex.com/very/long/path/continues/here/" + "x" * 41
    assert "\n" not in hrefs[0]


def test_markdown_link_shows_label_only():
    """The label-only rendering must survive the split (OSC 8 arrives as markdown)."""
    probe_src = (
        "const { renderCaptureLines } = await import(process.argv[1]);"
        "process.stdout.write(JSON.stringify("
        'renderCaptureLines("see [PR #7](https://x.io/pr/7) done\\ntail", {})));'
    )
    res = subprocess.run(
        ["node", "--input-type=module", "-e", probe_src, TERMINAL_JS.as_uri()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the assert below reports node's stderr on failure
    )
    assert res.returncode == 0, res.stderr
    lines = json.loads(res.stdout)
    assert len(lines) == 2
    assert 'href="https://x.io/pr/7"' in lines[0]
    assert ">PR #7</a>" in lines[0]
    assert "](https://x.io/pr/7)" not in lines[0], "the URL must stay hidden"
    assert lines[1] == "tail"
