"""Parse the web modules as ES modules, with the real parser.

`node --check <file>.js` does NOT do this: for a bare .js file it checks
CommonJS-ish syntax and blesses files the browser refuses to load. A stray
brace shipped that way once — the app parsed under node --check and died in
Chrome with 'Missing catch or finally after try', showing Connecting… forever.
Copying to .mjs forces node's ESM parser, which is the grammar that matters.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", ["app.js", "terminal.js"])
def test_module_parses_as_esm(tmp_path, name):
    mjs = tmp_path / (name + ".mjs")
    mjs.write_bytes((WEB / name).read_bytes())
    res = subprocess.run(
        ["node", "--check", str(mjs)], capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr
