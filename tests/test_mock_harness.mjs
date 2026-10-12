import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// The harness is a classic script (scratch cannot load modules); without a document it
// only exports its pure helpers.
const ctx = {};
vm.runInNewContext(readFileSync(new URL("../scripts/mock/harness.js", import.meta.url), "utf8"), ctx);
const { fits, widest, srcdoc } = ctx.mockHarness;

test("two frames sit side by side only when both fit at full width", () => {
  assert.equal(fits(390, 1920), true);
  assert.equal(fits(1280, 1920), false); // never squeezed into half the screen
  assert.equal(fits(952, 1920), true);
});

test("the first look is the widest preset that fits, else the narrowest", () => {
  assert.equal(widest([390, 1280, 1440, 1920], 1500), 1440);
  assert.equal(widest([390, 1280, 1440, 1920], 300), 390);
});

test("a frame's document inlines its stylesheet and images and runs the variant last", () => {
  const M = { css: ["a{background:url(@@IMG0@@)}"], imgs: ["data:image/png;base64,AA"] };
  const doc = srcdoc(M, { html: '<html><head></head><body><img src="@@IMG0@@"></body></html>', css: 0 },
    { variant: "a" }, 'x("</script>")');
  assert.match(doc, /^<!doctype html>/);
  assert.equal(doc.match(/data:image\/png;base64,AA/g).length, 2);
  assert.match(doc, /<style>a\{background:url\(data:/);
  assert.match(doc, /window\.MOCK = \{"variant":"a"\};/);
  assert.match(doc, /x\("<\\\/script>"\)<\/script><\/body>/); // a script's own </script> cannot end the tag
});
