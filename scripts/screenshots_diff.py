"""Screenshot the demo fleet on a base ref and on this tree, and diff them shot by shot.

    uv run python -m scripts.screenshots_diff [base-ref] [out-dir]    (or: make screenshots-diff)

The base is checked out as a throwaway worktree, and this tree's demo scripts are copied
into it, so both sides render the SAME fleet and only the UI under test differs — a base
that predates the demo dataset still diffs. Writes <out>/{base,head,diff}/*.png and
<out>/summary.md, a table of changed-pixel percentages to paste into a PR body.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
DEMO = ("demo_fleet.py", "demo_server.py", "screenshots.mjs")
NOISE = 0.05  # % of a shot; below it a difference is reported but the shot counts as unchanged


def shoot(tree: Path, out: Path) -> None:
    subprocess.run(["node", str(tree / "scripts/screenshots.mjs"), str(out), str(tree)],
                   check=True, cwd=tree,  # each tree runs its own venv, not the caller's
                   env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"})


def diff(base: Path, head: Path, out: Path) -> list[tuple[str, str, bool]]:
    """(shot, changed %, changed at all) rows. The diff image is the head shot faded, with
    every changed pixel painted red, so a reviewer sees what moved and where in one picture."""
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in sorted({p.name for p in base.glob("*.png")} | {p.name for p in head.glob("*.png")}):
        if not (base / name).exists() or not (head / name).exists():
            rows.append((name, "new" if (head / name).exists() else "removed", True))
            continue
        a, b = (Image.open(d / name).convert("RGB") for d in (base, head))
        if a.size != b.size:
            rows.append((name, f"size {a.size} -> {b.size}", True))
            continue
        mask = changed_mask(a, b)
        pct = 100 * mask.histogram()[255] / (a.width * a.height)
        faded = Image.blend(b, Image.new("RGB", b.size, "white"), 0.7)
        faded.paste(Image.new("RGB", b.size, (220, 0, 0)), mask=mask)
        faded.save(out / name)
        rows.append((name, f"{pct:.2f}%" + (" (noise)" if 0 < pct < NOISE else ""), pct >= NOISE))
    return rows


def changed_mask(a: Image.Image, b: Image.Image) -> Image.Image:
    """Pixels where either shot leaves the range of the other's 3x3 neighbourhood by more
    than 16 levels in some channel. A pixel that merely moved one device pixel, which is
    what anti-aliasing and glyph rasterization jitter look like, stays inside that range;
    anything that appeared, vanished, recoloured or moved further does not."""
    def beyond(x: Image.Image, y: Image.Image) -> Image.Image:
        low, high = y.filter(ImageFilter.MinFilter(3)), y.filter(ImageFilter.MaxFilter(3))
        return ImageChops.lighter(ImageChops.subtract(x, high), ImageChops.subtract(low, x))
    channels = ImageChops.lighter(beyond(a, b), beyond(b, a)).split()
    worst = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    return worst.point(lambda v: 255 if v > 16 else 0)


def images(out: Path, name: str) -> str:
    """A table row of base, head and diff, linked relative to summary.md so it previews as
    is; to paste it into a PR, prefix each src with wherever the PNGs were pushed (CI does)."""
    cells = (f'<img src="{sub}/{name}" width="280">' if (out / sub / name).exists() else "-"
             for sub in ("base", "head", "diff"))
    return f"| {name.removesuffix('.png')} | {' | '.join(cells)} |"


def main() -> None:
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    # Absolute: each side shoots with its own tree as cwd, the base in a temp worktree.
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / ".screenshots/diff").resolve()
    for sub in ("base", "head", "diff"):  # only what this tool writes; out may be anywhere
        shutil.rmtree(out / sub, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="tmux-rc-shots-base-") as tmp:
        base = Path(tmp) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(base), ref],
                       check=True)
        try:
            for name in DEMO:
                shutil.copy(ROOT / "scripts" / name, base / "scripts" / name)
            shoot(base, out / "base")
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(base)],
                           check=False)
    shoot(ROOT, out / "head")
    rows = diff(out / "base", out / "head", out / "diff")
    changed = [images(out, name) for name, _, moved in rows if moved]
    summary = "\n".join([f"| shot | changed vs `{ref}` |", "| --- | --- |",
                         *(f"| {name.removesuffix('.png')} | {pct} |" for name, pct, _ in rows),
                         *(["", "| changed | base | head | diff |", "| --- | --- | --- | --- |",
                            *changed] if changed else [])])
    (out / "summary.md").write_text(summary + "\n")
    print(summary)


if __name__ == "__main__":
    main()
