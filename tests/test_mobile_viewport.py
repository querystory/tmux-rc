"""Pin the two physical iPhone installation geometries and keyboard sizing."""

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
const root = {style: {setProperty: (key, value) => properties[key] = value},
  classList: {toggle: (key, value) => classes[key] = value}};
const viewport = {height: 873, offsetTop: 0, scale: 1};
const document = {documentElement: root, activeElement: null};
const navigator = {standalone: true};
let topInset = '0px';
const sandbox = {document, navigator, window: {visualViewport: viewport},
  matchMedia: () => ({matches: false}), $: () => ({}),
  getComputedStyle: () => ({paddingTop: topInset})};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
const fit = () => vm.runInContext('fitViewport()', sandbox);
// Opaque Safari-installed status bar: 873px usable, no top safe area.
fit(); assert.equal(properties['--app-height'], '873px');
assert.equal(classes['standalone-fill'], true);
// Older translucent installation: same reported viewport, plus 59px safe area.
topInset = '59px'; fit(); assert.equal(properties['--app-height'], '932px');
// Keyboard follows the visual viewport without the browsing compensation.
document.activeElement = {tagName: 'TEXTAREA'};
viewport.height = 440; viewport.offsetTop = 12;
fit(); assert.equal(properties['--app-height'], '440px');
assert.equal(properties['--app-top'], '12px');
assert.equal(classes['standalone-fill'], false);
// Returning from the keyboard restores the measured installed height.
document.activeElement = null; viewport.height = 873; viewport.offsetTop = 0;
fit(); assert.equal(properties['--app-height'], '932px');
// Normal browser tabs never receive standalone compensation.
navigator.standalone = false; fit();
assert.equal(properties['--app-height'], '873px');
assert.equal(classes['standalone-fill'], false);
// Pinch zoom must not resize the layout to a zoomed visual viewport.
viewport.height = 300; viewport.scale = 2; fit();
assert.equal(properties['--app-height'], '873px');
"""
    subprocess.run(
        ["node", "-e", script, str(Path(__file__).parents[1] / "web/m/app.js")],
        check=True, capture_output=True, text=True, timeout=10,
    )
