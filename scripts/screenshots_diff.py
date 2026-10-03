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

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
DEMO = ("demo_fleet.py", "demo_server.py", "screenshots.mjs")


def shoot(tree: Path, out: Path) -> None:
    subprocess.run(["node", str(tree / "scripts/screenshots.mjs"), str(out), str(tree)],
                   check=True, cwd=tree,  # each tree runs its own venv, not the caller's
                   env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"})


def diff(base: Path, head: Path, out: Path) -> list[tuple[str, str]]:
    """(shot, changed %) rows. The diff image is the head shot faded, with every changed
    pixel painted red, so a reviewer sees what moved and where in one picture."""
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in sorted({p.name for p in base.glob("*.png")} | {p.name for p in head.glob("*.png")}):
        if not (base / name).exists() or not (head / name).exists():
            rows.append((name, "new" if (head / name).exists() else "removed"))
            continue
        a, b = (Image.open(d / name).convert("RGB") for d in (base, head))
        if a.size != b.size:
            rows.append((name, f"size {a.size} -> {b.size}"))
            continue
        mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 16 else 0)
        changed = mask.histogram()[255]
        faded = Image.blend(b, Image.new("RGB", b.size, "white"), 0.7)
        faded.paste(Image.new("RGB", b.size, (220, 0, 0)), mask=mask)
        faded.save(out / name)
        rows.append((name, f"{100 * changed / (a.width * a.height):.2f}%"))
    return rows


def main() -> None:
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / ".screenshots/diff")
    shutil.rmtree(out, ignore_errors=True)
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
    summary = "\n".join([f"| shot | changed vs `{ref}` |", "| --- | --- |",
                         *(f"| {name.removesuffix('.png')} | {pct} |" for name, pct in rows)])
    (out / "summary.md").write_text(summary + "\n")
    print(summary)


if __name__ == "__main__":
    main()
