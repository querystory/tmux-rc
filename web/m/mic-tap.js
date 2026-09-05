// A same-origin worklet file is required for microphone capture on iOS.
registerProcessor("mobile-mic", class extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
});
