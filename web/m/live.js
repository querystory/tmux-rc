// Audio wire contract mirrors lmCapture/lmPlayChunk in /app.js. Keep rates, resampling,
// PCM scaling, and base64 chunk bounds in sync with those desktop implementations.
const CAPTURE_RATE = 16000; // Wire rate the server expects for mic PCM.
const PLAYBACK_RATE = 24000; // Rate of the PCM the server streams back.
const MIN_FRAME_SAMPLES = 4096; // Batch mic samples so each WebSocket frame is worth its JSON overhead.
const MAX_SOCKET_BACKLOG = 65536; // Drop mic audio once this much is unsent, instead of piling up latency.
const CHAR_CHUNK = 0x8000; // Same fromCharCode chunk bound as desktop lmCapture.
const CONNECT_DEADLINE_MS = 30000; // Give up if the server never reports "listening".
const MAX_RECONNECT_TRIES = 5; // Exponential backoff attempts before declaring the session lost.
const TRANSCRIPT_ROWS = 40; // Oldest transcript rows are dropped past this count.
const FOLLOW_SLACK_PX = 48; // Keep auto-scrolling while the log is within this distance of the bottom.

// Fallback glyphs used when the caller does not pass app.js's `licon` helper.
const FALLBACK_ICONS = {
  mic: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3ZM19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/></svg>',
  x: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg>',
};
const fallbackIcon = (name) => FALLBACK_ICONS[name];

export function setupLiveMode({ request, session, licon = fallbackIcon, onVersion = () => {} }) {
  const $ = (id) => document.getElementById(id);
  const mic = licon("mic");
  $("live-mode").innerHTML = $("voice-mute").innerHTML = mic;
  $("voice-close").innerHTML = licon("x");
  let run = null, sequence = 0, fetching = false, modelSignature = null;
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
    if (fetching) return;
    fetching = true;
    try {
      const data = await request("/api/version");
      onVersion(data.version);
      $("live-mode").hidden = !data.live_enabled && !run;
      if (run) return;
      const models = data.live_models || [{ label: "Default", value: "" }];
      let saved; try { saved = localStorage.getItem("tmuxrc-live-model"); } catch {}
      const signature = JSON.stringify([models, saved]);
      if (signature === modelSignature) return;
      modelSignature = signature;
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
    finally { fetching = false; }
  }
  function add(role, message, newSegment = false) {
    const log = $("voice-log"), previous = log.lastElementChild;
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < FOLLOW_SLACK_PX;
    const grow = !newSegment && (role === "user" || role === "model") && previous?.dataset.role === role && !previous.dataset.done;
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
    while (log.children.length > TRANSCRIPT_ROWS) log.firstChild.remove();
    if (follow) log.scrollTop = log.scrollHeight;
  }
  function silence(current) {
    current.queued.forEach((source) => { try { source.stop(); } catch {} });
    current.queued.clear(); current.playAt = 0;
  }
  async function keepAwake(current) {
    if (run !== current || document.hidden || !navigator.wakeLock || current.wakePending ||
        (current.wakeLock && !current.wakeLock.released)) return;
    current.wakePending = true;
    try {
      const lock = await navigator.wakeLock.request("screen");
      if (run !== current || document.hidden) await lock.release();
      else current.wakeLock = lock;
    } catch { /* Optional: low-power mode or browser policy can deny wake locks. */ }
    finally { current.wakePending = false; }
  }
  function audioStatus(current) {
    if (run !== current) return;
    const tracks = current.stream?.getAudioTracks() || [];
    const interrupted = current.capture?.state !== "running" ||
      tracks.some((track) => track.muted) || current.output?.paused || current.audioSession?.state === "interrupted";
    if (!interrupted && current.stream) current.resolveReady?.();
    if (!current.listening && !current.stream) return;
    status(interrupted ? "Audio interrupted. Return to the app and tap the microphone to resume." :
      current.muted ? "Microphone muted" : current.listening ? `Listening / ${current.model || "Default"}` : "Connecting...");
  }
  function resumeAudio(current, userGesture = false) {
    audioStatus(current);
    if (run !== current || (current.resuming && !userGesture) || !current.stream) return;
    const attempt = {}; current.resuming = attempt;
    // Try once per lifecycle/state event, including when backgrounded. WebKit
    // can permit resume while capturing; never spin if the OS denies it.
    const retries = [];
    if (current.capture.state !== "running" && current.capture.state !== "closed") retries.push(current.capture.resume());
    if (current.output.paused) retries.push(current.output.play());
    Promise.allSettled(retries).finally(() => {
      if (current.resuming === attempt) current.resuming = null;
      audioStatus(current);
    });
  }
  function watchAudio(current) {
    const changed = () => { audioStatus(current); resumeAudio(current); };
    current.capture.onstatechange = changed;
    current.output.onpause = changed;
    current.output.onplaying = changed;
    for (const track of current.stream.getAudioTracks()) {
      track.onmute = changed;
      track.onunmute = changed;
      track.onended = () => { if (run === current) stop("Microphone disconnected. Start Live Mode again."); };
      if (track.readyState === "ended") { track.onended(); return; }
    }
    current.audioChanged = changed;
    current.audioSession?.addEventListener("statechange", changed);
    changed();
  }
  function stop(message = "Session ended") {
    const current = run; run = null; sequence++;
    if (current) {
      clearTimeout(current.retry); clearTimeout(current.deadline);
      current.resolveReady?.();
      current.wakeLock?.release().catch(() => {});
      if (current.ws?.readyState === WebSocket.OPEN) {
        try { current.ws.send(JSON.stringify({ action: "stop" })); } catch {}
      }
      try { current.ws?.close(); } catch {}
      if (current.audioChanged) current.audioSession?.removeEventListener("statechange", current.audioChanged);
      // Release our audio category without overwriting a change made elsewhere.
      try {
        if (current.audioSession?.type === "play-and-record") current.audioSession.type = current.previousAudioType;
      } catch { /* Audio Session is optional. */ }
      current.stream?.getTracks().forEach((track) => {
        track.onmute = track.onunmute = track.onended = null; track.stop();
      });
      if (current.capture) current.capture.onstatechange = null;
      if (current.output) {
        current.output.onpause = current.output.onplaying = null;
        current.output.pause(); current.output.srcObject = null;
      }
      current.destination?.stream.getTracks().forEach((track) => track.stop());
      current.destination?.disconnect();
      current.nodes.forEach((node) => { try { node.disconnect(); } catch {} });
      silence(current);
      current.capture?.close().catch(() => {});
    }
    status(message); paint();
  }
  function playAudio(current, data, sampleRate = PLAYBACK_RATE) {
    const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer);
    const buffer = current.play.createBuffer(1, pcm.length, sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 0x8000;
    const source = current.play.createBufferSource(); source.buffer = buffer;
    source.connect(current.destination); current.queued.add(source);
    source.onended = () => current.queued.delete(source);
    current.playAt = Math.max(current.playAt, current.play.currentTime);
    source.start(current.playAt); current.playAt += buffer.duration;
  }
  async function capture(current) {
    // The tap worklet is shared with the desktop Live Mode; see lm-tap.js for why it
    // has to be a same-origin file rather than an inline blob: module.
    await current.capture.audioWorklet.addModule("/lm-tap.js");
    if (run !== current) return;
    const source = current.capture.createMediaStreamSource(current.stream);
    const tap = new AudioWorkletNode(current.capture, "lm-tap");
    const mute = current.capture.createGain(); mute.gain.value = 0;
    const rate = current.capture.sampleRate;
    let pending = new Float32Array(0);
    current.clearPending = () => { pending = new Float32Array(0); };
    tap.port.onmessage = ({ data }) => {
      if (run !== current || !current.listening || (current.muted && !current.frameMs) || current.ws?.readyState !== WebSocket.OPEN || current.ws.bufferedAmount > MAX_SOCKET_BACKLOG) { pending = new Float32Array(0); return; }
      const joined = new Float32Array(pending.length + data.length);
      joined.set(pending);
      // A worklet message captured before the toggle can arrive after mute.
      // Float32Array already appended data.length ZERO samples: the buffer still
      // grows while muted, preserving GPT-Live framing without copying mic audio.
      if (!current.muted) joined.set(data, pending.length);
      pending = joined;
      if (pending.length < (current.frameMs ? rate * current.frameMs / 1000 : MIN_FRAME_SAMPLES)) return;
      let samples = pending; pending = new Float32Array(0);
      if (rate !== CAPTURE_RATE) {
        const resampled = new Float32Array(Math.round(samples.length * CAPTURE_RATE / rate));
        for (let i = 0; i < resampled.length; i++) {
          const at = i * (samples.length - 1) / (resampled.length - 1), low = Math.floor(at);
          resampled[i] = samples[low] + (samples[Math.min(low + 1, samples.length - 1)] - samples[low]) * (at - low);
        }
        samples = resampled;
      }
      const pcm = new Int16Array(samples.length);
      for (let i = 0; i < samples.length; i++) pcm[i] = Math.max(-1, Math.min(1, samples[i])) * 0x7fff;
      const bytes = new Uint8Array(pcm.buffer);
      let binary = "";
      for (let i = 0; i < bytes.length; i += CHAR_CHUNK) binary += String.fromCharCode(...bytes.subarray(i, i + CHAR_CHUNK));
      current.ws.send(JSON.stringify({ action: "audio", data: btoa(binary) }));
    };
    source.connect(tap); tap.connect(mute); mute.connect(current.destination);
    current.nodes = [source, tap, mute];
  }
  function connect(current) {
    if (run !== current) return;
    const query = new URLSearchParams();
    if (session) query.set("session", session);
    if (current.model) query.set("model", current.model);
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live-mode?${query}`); }
    catch { stop("Could not connect to Live Mode."); return; }
    current.ws = ws;
    clearTimeout(current.deadline);
    current.deadline = setTimeout(() => { if (run === current && !current.listening) stop("Live Mode connection timed out. Try again."); }, CONNECT_DEADLINE_MS);
    ws.onmessage = ({ data }) => {
      if (run !== current || current.ws !== ws) return;
      let message; try { message = JSON.parse(data); } catch { return; }
      if (message.type === "status") {
        current.frameMs = message.frame_ms;
        current.up = true; current.listening = message.status === "listening";
        if (current.listening) { clearTimeout(current.deadline); current.tries = 0; }
        if (current.listening) audioStatus(current);
        else status(message.status === "reconnecting" ? "Reconnecting..." : "Connecting...");
      } else if (message.type === "transcript") add(message.role, message.text, message.new_segment);
      else if (message.type === "turn_complete") [...$("voice-log").children].forEach((row) => { row.dataset.done = "true"; });
      else if (message.type === "typed") add("typed", `${message.label} (${message.pane_id})${message.submitted ? "" : " (not submitted)"}: ${message.text}`);
      else if (message.type === "error") add("error", message.message);
      else if (message.type === "interrupted") silence(current);
      else if (message.type === "audio") { try { playAudio(current, message.data, message.sample_rate); } catch { add("error", "Could not play this audio chunk."); } }
    };
    ws.onclose = (event) => {
      if (run !== current || current.ws !== ws) return;
      clearTimeout(current.deadline); current.listening = false;
      if (event.code !== 1000 && event.code !== 1005 && current.up && current.tries < MAX_RECONNECT_TRIES) {
        status("Connection lost. Reconnecting...");
        current.retry = setTimeout(() => connect(current), 1000 * 2 ** current.tries++);
      } else stop(event.code === 1000 ? "Session ended" : "Live Mode disconnected. Try again.");
    };
  }
  async function start() {
    const token = ++sequence;
    const current = { model: $("voice-model").value, nodes: [], queued: new Set(), playAt: 0, tries: 0, up: false, listening: false, muted: false };
    run = current; $("voice-log").replaceChildren(); status("Connecting microphone..."); paint();
    current.deadline = setTimeout(() => {
      if (run === current) stop("Microphone setup timed out. Start Live Mode again.");
    }, CONNECT_DEADLINE_MS);
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone capture requires HTTPS and a supported browser.");
      // Tell supporting browsers this is a duplex conversation before opening
      // the mic. This requests appropriate audio focus, not background permission.
      try {
        const audioSession = navigator.audioSession;
        if (audioSession) {
          const previous = audioSession.type;
          audioSession.type = "play-and-record";
          current.audioSession = audioSession; current.previousAudioType = previous;
        }
      } catch { /* Browsers without Audio Session keep their default routing. */ }
      // WebKit's background exemption uses a processing context with a media
      // stream destination, not the hardware destination (WebKit bug 231105).
      // One duplex graph drives real assistant playback through an audio element.
      current.play = current.capture = new AudioContext();
      current.destination = current.capture.createMediaStreamDestination();
      current.output = new Audio(); current.output.srcObject = current.destination.stream;
      // A second resume/play can succeed while an earlier request stays pending.
      // Observe actual state changes instead of awaiting those original promises.
      const ready = new Promise((resolve) => { current.resolveReady = resolve; });
      current.capture.resume().catch(() => audioStatus(current));
      current.output.play().catch(() => audioStatus(current));
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
      if (sequence !== token) { stream.getTracks().forEach((track) => track.stop()); return; }
      current.stream = stream; watchAudio(current);
      if (run !== current) return;
      paint();
      keepAwake(current);
      await capture(current);
      if (run !== current) return;
      await ready;
      if (run !== current) return;
      try { localStorage.setItem("tmuxrc-live-model", current.model); } catch {}
      status("Connecting..."); connect(current);
    } catch (error) {
      if (run !== current) return;
      stop(`${error.name === "NotAllowedError" ? "Microphone access denied. Allow microphone access for this site." : "Live Mode could not start: " + error.message}`);
    }
  }
  $("live-mode").onclick = () => { $("voice-dialog").showModal(); if (run) resumeAudio(run, true); };
  $("voice-close").onclick = () => $("voice-dialog").close();
  $("voice-start").onclick = () => run ? stop() : start();
  $("voice-mute").onclick = () => {
    if (!run?.stream) return;
    run.muted = !run.muted;
    run.clearPending?.();
    run.stream.getAudioTracks().forEach((track) => { track.enabled = !run.muted; });
    paint(); audioStatus(run); resumeAudio(run, true);
  };
  window.addEventListener("pagehide", () => stop());
  window.addEventListener("online", capabilities);
  // Switching apps is visibilitychange, not navigation: keep the microphone and
  // socket alive. pagehide still releases capture when leaving this document.
  window.addEventListener("pageshow", () => { if (run) { resumeAudio(run); keepAwake(run); } });
  document.addEventListener("visibilitychange", () => {
    if (run) { resumeAudio(run); keepAwake(run); }
    if (!document.hidden) capabilities();
  });
  capabilities();
  return { isActive: () => !!run, refresh: capabilities };
}
