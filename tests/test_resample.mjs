import assert from "node:assert/strict";
import { test } from "node:test";
import { downsampler } from "../web/m/resample.js";

// Feed a tone through in batches (40 ms, as GPT-Live frames the mic, unless given), and join the output.
const run = (from, hz, seconds = 2, frame = from * 0.04) => {
  const down = downsampler(from, 16000), parts = [];
  for (let start = 0; start < from * seconds; start += frame)
    parts.push(down(Float32Array.from({ length: frame }, (_, i) => Math.sin(2 * Math.PI * hz * (start + i) / from))));
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((at, p) => (out.set(p, at), at + p.length), 0);
  return out;
};
// Least-squares fit of a sinusoid at `hz` (16 kHz samples): its amplitude and the worst residual.
const fit = (x, hz) => {
  let c = 0, s = 0;
  for (let n = 0; n < x.length; n++) { c += x[n] * Math.cos(2 * Math.PI * hz * n / 16000); s += x[n] * Math.sin(2 * Math.PI * hz * n / 16000); }
  c *= 2 / x.length; s *= 2 / x.length;
  let worst = 0;
  for (let n = 0; n < x.length; n++) worst = Math.max(worst, Math.abs(x[n] - c * Math.cos(2 * Math.PI * hz * n / 16000) - s * Math.sin(2 * Math.PI * hz * n / 16000)));
  return { amplitude: Math.hypot(c, s), worst };
};
const settled = (x) => x.subarray(64, x.length - 64); // past the filter's ~1 ms start-up

for (const from of [48000, 44100]) {
  for (const frame of [from * 0.04, 1792, 4096]) test(`${from} Hz: a 1 kHz tone stays 1 kHz, full length, seamless across ${frame}-sample batches`, () => {
    const out = run(from, 1000, 2, frame);
    assert.ok(Math.abs(out.length - 32000) <= 20 + frame / 3, `${out.length} samples for 2 s`); // only the ~1 ms lag missing
    const { amplitude, worst } = fit(settled(out), 1000);
    assert.ok(Math.abs(amplitude - 1) < 0.01, `amplitude ${amplitude}`);
    assert.ok(worst < 0.01, `worst deviation from a clean 1 kHz sine ${worst}`); // a frame-edge glitch or warp would show here
  });

  test(`${from} Hz: a 12 kHz tone is filtered out instead of aliasing to 4 kHz`, () => {
    const out = settled(run(from, 12000));
    const alias = fit(out, 4000).amplitude;
    const rms = Math.sqrt(out.reduce((a, v) => a + v * v, 0) / out.length);
    assert.ok(alias < 0.001 && rms < 0.001, `alias ${alias}, rms ${rms}`); // below -60 dB
  });
}
