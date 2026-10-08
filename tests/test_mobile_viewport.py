"""Pin installed-app sizing: CSS at rest, the visual viewport only under the keyboard."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_installed_viewport_accounts_for_status_bar_mode():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function fitViewport()');
const end = source.indexOf('window.visualViewport?.addEventListener("resize", fitViewport)', start);
assert.ok(start >= 0 && end > start, 'viewport helper moved');
const properties = {}, classes = {};
const root = {style: {setProperty: (key, value) => properties[key] = value,
  removeProperty: (key) => delete properties[key]},
  classList: {toggle: (key, value) => classes[key] = value}};
const viewport = {height: 873, offsetTop: 0, scale: 1};
const document = {documentElement: root, activeElement: null};
const navigator = {standalone: true};
let displayModeStandalone = false;
const sandbox = {document, navigator, window: {visualViewport: viewport},
  matchMedia: () => ({matches: displayModeStandalone})};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
const fit = () => vm.runInContext('fitViewport()', sandbox);
// At rest an install fills by CSS alone (dvh plus the top safe area), never a measurement.
fit(); assert.deepEqual(properties, {});
assert.equal(classes['standalone-fill'], true);
// The standards-based installation path must work without Apple's property.
navigator.standalone = false; displayModeStandalone = true; fit();
assert.equal(classes['standalone-fill'], true);
// Keyboard follows the visual viewport.
document.activeElement = {tagName: 'TEXTAREA'};
viewport.height = 440; viewport.offsetTop = 12;
fit(); assert.equal(properties['--app-height'], '440px');
assert.equal(properties['--app-top'], '12px');
assert.equal(classes['standalone-fill'], false);
// Leaving the editor drops the keyboard-height measurement even if the visual viewport
// has not grown back yet (mid-dismissal, backgrounded): nothing stale can survive.
document.activeElement = null; fit(); assert.deepEqual(properties, {});
assert.equal(classes['standalone-fill'], true);
// Normal browser tabs never receive standalone compensation.
navigator.standalone = false; displayModeStandalone = false; fit();
assert.deepEqual(properties, {});
assert.equal(classes['standalone-fill'], false);
// Pinch zoom must not resize the layout to a zoomed visual viewport.
document.activeElement = {tagName: 'TEXTAREA'}; viewport.height = 300; viewport.scale = 2; fit();
assert.deepEqual(properties, {});
"""
    subprocess.run(
        ["node", "-e", script, str(Path(__file__).parents[1] / "web/m/app.js")],
        check=True, capture_output=True, text=True, timeout=10,
    )
