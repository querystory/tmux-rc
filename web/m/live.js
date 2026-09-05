// Same PCM and WebSocket protocol as the full UI's Live Mode.
export function setupLiveMode({ request, session }) {
  const $ = (id) => document.getElementById(id);
  const mic = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3ZM19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/></svg>';
  $("live-mode").innerHTML = $("voice-mute").innerHTML = mic;
  $("voice-close").innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg>';
  let run = null, sequence = 0;
  const status = (message) => { $("voice-status").textContent = message; };
  function paint() {
    $("live-mode").classList.toggle("active", !!run);
    $("live-mode").title = $("live-mode").ariaLabel = run ? "Live Mode active" : "Live Mode";
    $("voice-start").textContent = run ? "End Live Mode" : "Start Live Mode";
    $("voice-start").hidden = !run;
    $("voice-models").hidden = !!run;
    $("voice-mute").hidden = !run?.stream;
    $("voice-mute").setAttribute("aria-pressed", !!run?.muted);
    $("voice-mute").title = $("voice-mute").ariaLabel = run?.muted ? "Unmute microphone" : "Mute microphone";
  }
  async function capabilities() {
    try {
      const data = await request("/api/version");
      $("live-mode").hidden = !data.live_enabled && !run;
      if (run) return;
      const models = data.live_models || [{ label: "Default", value: "" }];
      let saved; try { saved = localStorage.getItem("tmuxrc-live-model"); } catch {}
      $("voice-models").replaceChildren(...models.map((model) => {
        const button = document.createElement("button");
        const image = document.createElement("img"); image.alt = "";
        image.src = /gemini/i.test(model.label) ? "/gemini.svg" : /gpt|openai/i.test(model.label) ? "/openai.svg" : "/icon.svg";
        const label = document.createElement("span"), title = document.createElement("strong"), hint = document.createElement("small");
        title.textContent = model.label; hint.textContent = [model.hint, (model.value ?? model.label) === saved ? "Last used" : ""].filter(Boolean).join(" / ");
        label.append(title, hint); button.append(image, label); button.insertAdjacentHTML("beforeend", mic);
        button.onclick = () => { $("voice-model").value = model.value ?? model.label; start(); };
        return button;
      }));
    } catch { /* Retain the last confirmed capabilities during a tunnel reconnect. */ }
  }
  function add(role, message) {
    const log = $("voice-log"), previous = log.lastElementChild;
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    const grow = (role === "user" || role === "model") && previous?.dataset.role === role && !previous.dataset.done;
    let row = previous;
    if (!grow) {
      row = document.createElement("div"); row.className = "voice-entry";
      row.dataset.role = role;
      row.classList.add(["user", "model", "typed", "error"].includes(role) ? role : "model");
      const heading = document.createElement("strong");
      heading.textContent = { user: "You", model: "Assistant", typed: "Sent to terminal", error: "Connection" }[role] || "Assistant";
      row.append(heading, document.createElement("span")); log.append(row);
    }
    row.lastChild.textContent += message || "";
    while (log.children.length > 40) log.firstChild.remove();
    if (follow) log.scrollTop = log.scrollHeight;
  }
  function silence(current) {
    current.queued.forEach((source) => { try { source.stop(); } catch {} });
    current.queued.clear(); current.playAt = 0;
  }
  function stop(message = "Session ended") {
    const current = run; run = null; sequence++;
    if (current) {
      clearTimeout(current.retry); clearTimeout(current.deadline);
      if (current.ws?.readyState === WebSocket.OPEN) {
        try { current.ws.send(JSON.stringify({ action: "stop" })); } catch {}
      }
      try { current.ws?.close(); } catch {}
      current.stream?.getTracks().forEach((track) => track.stop());
      current.nodes.forEach((node) => { try { node.disconnect(); } catch {} });
      silence(current);
      current.capture?.close().catch(() => {});
      current.play?.close().catch(() => {});
    }
    status(message); paint();
  }
  function playAudio(current, data) {
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer);
    const buffer = current.play.createBuffer(1, pcm.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
    const source = current.play.createBufferSource(); source.buffer = buffer;
    source.connect(current.play.destination); current.queued.add(source);
    source.onended = () => current.queued.delete(source);
    current.playAt = Math.max(current.playAt, current.play.currentTime);
    source.start(current.playAt); current.playAt += buffer.duration;
  }
  async function capture(current) {
    await current.capture.audioWorklet.addModule("/m/mic-tap.js");
    if (run !== current) return;
    const source = current.capture.createMediaStreamSource(current.stream);
    const tap = new AudioWorkletNode(current.capture, "mobile-mic");
    const mute = current.capture.createGain(); mute.gain.value = 0;
    const rate = current.capture.sampleRate;
    let pending = new Float32Array(0);
    tap.port.onmessage = ({ data }) => {
      if (run !== current || !current.listening || current.muted || current.ws?.readyState !== WebSocket.OPEN || current.ws.bufferedAmount > 65536) { pending = new Float32Array(0); return; }
      const joined = new Float32Array(pending.length + data.length);
      joined.set(pending); joined.set(data, pending.length); pending = joined;
      if (pending.length < 4096) return;
      let samples = pending; pending = new Float32Array(0);
      if (rate !== 16000) {
        const resampled = new Float32Array(Math.round(samples.length * 16000 / rate));
        for (let i = 0; i < resampled.length; i++) {
          const at = i * (samples.length - 1) / (resampled.length - 1), low = Math.floor(at);
          resampled[i] = samples[low] + (samples[Math.min(low + 1, samples.length - 1)] - samples[low]) * (at - low);
        }
        samples = resampled;
      }
      const pcm = new Int16Array(samples.length);
      for (let i = 0; i < samples.length; i++) pcm[i] = Math.max(-1, Math.min(1, samples[i])) * 32767;
      const bytes = new Uint8Array(pcm.buffer);
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      current.ws.send(JSON.stringify({ action: "audio", data: btoa(binary) }));
    };
    source.connect(tap); tap.connect(mute); mute.connect(current.capture.destination);
    current.nodes = [source, tap, mute];
  }
  function connect(current) {
    if (run !== current) return;
    const query = new URLSearchParams({ session });
    if (current.model) query.set("model", current.model);
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live-mode?${query}`); }
    catch { stop("Could not connect to Live Mode."); return; }
    current.ws = ws;
    current.deadline = setTimeout(() => { if (run === current && !current.listening) stop("Live Mode connection timed out. Try again."); }, 30000);
    ws.onmessage = ({ data }) => {
      if (run !== current || current.ws !== ws) return;
      let message; try { message = JSON.parse(data); } catch { return; }
      if (message.type === "status") {
        current.up = true; current.listening = message.status === "listening";
        if (current.listening) { clearTimeout(current.deadline); current.tries = 0; }
        status(current.listening ? `Listening / ${current.model || "Default"}` : message.status === "reconnecting" ? "Reconnecting..." : "Connecting...");
      } else if (message.type === "transcript") add(message.role, message.text);
      else if (message.type === "turn_complete") [...$("voice-log").children].forEach((row) => { row.dataset.done = "true"; });
      else if (message.type === "typed") add("typed", `${message.label} (${message.pane_id})${message.submitted ? "" : " (not submitted)"}: ${message.text}`);
      else if (message.type === "error") add("error", message.message);
      else if (message.type === "interrupted") silence(current);
      else if (message.type === "audio") { try { playAudio(current, message.data); } catch { add("error", "Could not play this audio chunk."); } }
    };
    ws.onclose = (event) => {
      if (run !== current || current.ws !== ws) return;
      clearTimeout(current.deadline); current.listening = false;
      if (event.code !== 1000 && event.code !== 1005 && current.up && current.tries < 5) {
        status("Connection lost. Reconnecting...");
        current.retry = setTimeout(() => connect(current), 1000 * 2 ** current.tries++);
      } else stop(event.code === 1000 ? "Session ended" : "Live Mode disconnected. Try again.");
    };
  }
  async function start() {
    const token = ++sequence;
    const current = { model: $("voice-model").value, nodes: [], queued: new Set(), playAt: 0, tries: 0, up: false, listening: false, muted: false };
    run = current; $("voice-log").replaceChildren(); status("Connecting microphone..."); paint();
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone capture requires HTTPS and a supported browser.");
      // Both contexts are created and resumed within the tap's activation on iOS.
      current.play = new AudioContext();
      try { current.capture = new AudioContext({ sampleRate: 16000 }); } catch { current.capture = new AudioContext(); }
      const resumes = Promise.all([current.play.resume(), current.capture.resume()]);
      resumes.catch(() => {});
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
      if (sequence !== token) { stream.getTracks().forEach((track) => track.stop()); return; }
      current.stream = stream; paint();
      await resumes;
      if (run !== current) return;
      await capture(current);
      if (run !== current) return;
      try { localStorage.setItem("tmuxrc-live-model", current.model); } catch {}
      status("Connecting..."); connect(current);
    } catch (error) {
      if (run !== current) return;
      stop(`${error.name === "NotAllowedError" ? "Microphone access denied. Allow microphone access for this site." : "Live Mode could not start: " + error.message}`);
    }
  }
  $("live-mode").onclick = () => { $("voice-dialog").showModal(); };
  $("voice-close").onclick = () => $("voice-dialog").close();
  $("voice-start").onclick = () => run ? stop() : start();
  $("voice-mute").onclick = () => {
    if (!run?.stream) return;
    run.muted = !run.muted;
    run.stream.getAudioTracks().forEach((track) => { track.enabled = !run.muted; });
    paint();
  };
  window.addEventListener("pagehide", () => stop());
  window.addEventListener("online", capabilities);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) capabilities(); });
  capabilities();
}
