"""Render a tmux pane capture (with ANSI color) to a PNG for the vision LLM.

Text capture loses color, bold, and the highlighted selection — exactly the signal an
agent's UI uses (red errors, green diffs, the selected menu item). Sending a rendered
screenshot to Gemini preserves it. This is a deliberately small SGR parser: enough of
the escape grammar to color a monospace grid, not a full terminal emulator.
"""

from __future__ import annotations

import re

from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_CELL_W, _CELL_H, _FONT_SIZE = 8, 17, 14
_PAD = 8
_BG = (13, 17, 23)
_FG = (201, 209, 217)

# Standard 16-color ANSI palette (xterm-ish), indexed by SGR 30-37 / 90-97.
_PALETTE = [
    (30, 30, 30), (248, 81, 73), (63, 185, 80), (210, 153, 34),
    (56, 139, 253), (188, 140, 255), (57, 197, 187), (201, 209, 217),
    (110, 118, 129), (255, 123, 114), (86, 211, 100), (227, 179, 65),
    (121, 192, 255), (210, 168, 255), (86, 211, 187), (255, 255, 255),
]
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[\[\]][0-9;?]*[A-Za-z]")


def _cell(fg, bold):
    return _PALETTE[(fg or 7) + (8 if bold and fg is not None and fg < 8 else 0)] if fg is not None else (
        (255, 255, 255) if bold else _FG
    )


def render_png(ansi_text: str, cols: int = 120) -> bytes:
    """Render ANSI-colored capture text to a PNG (bytes). `ansi_text` should come from
    `tmux capture-pane -e` so SGR color codes are present."""
    lines = ansi_text.split("\n")
    rows = len(lines)
    img = Image.new("RGB", (_PAD * 2 + cols * _CELL_W, _PAD * 2 + rows * _CELL_H), _BG)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()

    for row, line in enumerate(lines):
        y = _PAD + row * _CELL_H
        col = 0
        fg: int | None = None
        bold = False
        i = 0
        # Walk the line, applying SGR codes and drawing runs of plain chars.
        while i < len(line):
            m = _SGR_RE.match(line, i)
            if m:
                for code in (m.group(1) or "0").split(";"):
                    c = int(code or 0)
                    if c == 0:
                        fg, bold = None, False
                    elif c == 1:
                        bold = True
                    elif c == 22:
                        bold = False
                    elif 30 <= c <= 37:
                        fg = c - 30
                    elif 90 <= c <= 97:
                        fg = c - 90 + 8
                    elif c == 39:
                        fg = None
                i = m.end()
                continue
            m2 = _OTHER_ESC_RE.match(line, i)
            if m2:  # skip cursor moves / OSC we don't render
                i = m2.end()
                continue
            ch = line[i]
            if ch != " " and col < cols:
                draw.text((_PAD + col * _CELL_W, y), ch, font=font, fill=_cell(fg, bold))
            col += 1
            i += 1

    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
