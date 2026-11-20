// Streaming mic downsampler (e.g. 48 kHz or 44.1 kHz to 16 kHz). Each output sample is a
// Blackman-windowed sinc low-pass evaluated at its exact input position, so it filters out
// what would alias (content above the output's Nyquist) and resamples any ratio in one
// step. The position and the input it still needs carry across calls, so frames join
// seamlessly. The output lags the input by half the kernel (~1 ms).
const ZEROS = 16; // sinc zero crossings each side, in output periods: ~-74 dB stopband
const PASS = 0.9; // cutoff as a fraction of the output Nyquist (7.2 kHz at 16 kHz)

export function downsampler(from, to) {
  // Positions are counted in 1/to input samples, integers, so every index below is exact.
  const step = from / to, half = ZEROS * from, cut = PASS * to / from; // cutoff over the input Nyquist
  let buf = new Float32Array(Math.ceil(half / to)), at = buf.length * to; // zero history, first output at input 0
  // The taps for one offset of the first tap from the output position: a single one for an
  // integer ratio like 48 kHz, 160 for 44.1 kHz, so each is computed once.
  const phases = new Map();
  const kernel = (offset) => {
    if (!phases.has(offset)) {
      const taps = [];
      for (let t = offset; t <= half; t += to) {
        const d = t / to, x = Math.PI * t / half;
        taps.push((d ? Math.sin(Math.PI * cut * d) / (Math.PI * d) : cut) * (0.42 + 0.5 * Math.cos(x) + 0.08 * Math.cos(2 * x)));
      }
      const gain = taps.reduce((a, b) => a + b); // normalized: DC passes at exactly unity whatever the phase
      phases.set(offset, Float32Array.from(taps, (h) => h / gain));
    }
    return phases.get(offset);
  };
  return (input) => {
    const joined = new Float32Array(buf.length + input.length);
    joined.set(buf); joined.set(input, buf.length);
    const out = new Float32Array(Math.max(0, Math.floor(((joined.length - 1) * to - half - at) / from) + 1));
    for (let k = 0; k < out.length; k++, at += from) {
      const first = Math.ceil((at - half) / to), taps = kernel(first * to - at);
      let sum = 0;
      for (let j = 0; j < taps.length; j++) sum += joined[first + j] * taps[j];
      out[k] = sum;
    }
    const keep = Math.floor((at - half) / to);
    buf = joined.slice(keep); at -= keep * to;
    return out;
  };
}
