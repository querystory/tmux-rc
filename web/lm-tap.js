// Live Mode mic tap. Served as a real same-origin file, not an inline blob: URL —
// WebKit (every iOS browser) refuses a blob: worklet module with "Cross-origin script
// load denied by Cross-Origin Resource Sharing policy", so capture never started on phones.
registerProcessor("lm-tap", class extends AudioWorkletProcessor {
  process(inputs) { const c = inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; }
});
