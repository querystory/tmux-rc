import { liveClose } from "/live-close.js";
import { chatBubble, chatComposer, chatThumb } from "/live-chat.js";
import { appendChatMarkdown } from "/chat-markdown.js";
import { chatStarters } from "/chat-starters.js";
import { since } from "/m/pane-model.js";
// Audio wire contract: rates, resampling, PCM scaling and base64 chunk bounds must match
// what the server expects (see docs/design/live-mode.md).
const CAPTURE_RATE = 16000; // Wire rate the server expects for mic PCM.
const PLAYBACK_RATE = 24000; // Rate of the PCM the server streams back.
const MIN_FRAME_SAMPLES = 4096; // Batch mic samples so each WebSocket frame is worth its JSON overhead.
const MAX_SOCKET_BACKLOG = 65536; // Drop mic audio once this much is unsent, instead of piling up latency.
const CHAR_CHUNK = 0x8000; // fromCharCode argument-count bound.
const CONNECT_DEADLINE_MS = 30000; // Give up (retry, while a card waits) if the server never reports "listening".
const MAX_RECONNECT_TRIES = 5; // Exponential backoff attempts before declaring the session lost; its cap while a card waits.
const TRANSCRIPT_ROWS = 40; // Oldest transcript rows are dropped past this count.
const FOLLOW_SLACK_PX = 48; // Keep auto-scrolling while the log is within this distance of the bottom.
const CHAT_MODEL_KEY = "tmuxrc-chat-model"; // Chat's last model, apart from the voice picker's

export function setupLiveMode({ request, session, licon, wide, open, report = () => {}, onVersion = () => {} }) {
  const $ = (id) => document.getElementById(id);
  const mic = licon("mic"), dialog = $("voice-dialog"), log = $("voice-log");
  $("live-mode").innerHTML = $("voice-mute").innerHTML = mic;
  $("voice-end").innerHTML = licon("x");
  $("chat").innerHTML = licon("message");
  let run = null, sequence = 0, fetching = false, modelSignature = null, menu = [], unread = false, composing = false;
  // Voice comes from the mic button, through the model picker. Chat (typed turns and
  // written replies, no mic and no playback) comes from the chat button, straight in.
  let mode = "voice";
  const status = (message) => { $("voice-status").textContent = message; };
  // "Live Mode" names only a voice session; the typed one is Chat, everywhere it shows.
  const name = (current) => (current.text ? "Chat" : "Live Mode");
  function paint() {
    const text = run ? run.text : mode === "text", title = name({ text });
    $("live-mode").classList.toggle("active", !!run && !run.text);
    $("chat").classList.toggle("active", !!run?.text);
    $("live-mode").title = $("live-mode").ariaLabel = run && !run.text ? "Live Mode active" : "Live Mode";
    $("voice-title").textContent = title;
    $("voice-start").textContent = `End ${title}`;
    $("voice-start").hidden = !run;
    $("voice-controls").hidden = !run || run.text; // a chat ends from the header X
    $("voice-end").hidden = !run?.text;
    $("voice-models").hidden = !!run || text;
    $("voice-switch").hidden = !run?.text || $("voice-switch").children.length < 2;
    $("voice-compose").hidden = !run;
    $("voice-mute").hidden = !run?.stream;
    $("voice-mute").setAttribute("aria-pressed", !!run?.muted);
    $("voice-mute").title = $("voice-mute").ariaLabel = run?.muted ? "Unmute microphone" : "Mute microphone";
    // Closing the sheet mid-conversation only minimizes it: say so, and show the bubble.
    $("voice-close").innerHTML = licon(run ? "minus" : "x");
    $("voice-close").title = $("voice-close").ariaLabel = run ? `Minimize ${title}` : "Close panel";
    paintStarters();
    badge();
  }
  const following = () => log.scrollHeight - log.scrollTop - log.clientHeight < FOLLOW_SLACK_PX;
  // Until a chat turn completes, an Assistant row of dots sits at the foot of the log and
  // rows that arrive meanwhile go above it. It stays up past a first reply, since a turn
  // can say something and then keep working (a find_sessions call shows nothing), and hides
  // while a consent card is open (the model is waiting on the user then).
  const typing = document.createElement("div");
  typing.className = "voice-entry model";
  typing.innerHTML = '<strong aria-hidden="true">Assistant</strong><span class="chat-dots" role="img" aria-label="Assistant is responding"><i></i><i></i><i></i></span>';
  function badge() {
    const working = run?.turns > 0 && !run.proposals.size;
    bubble({ shown: !!run && !dialog.open, voice: run && !run.text, unread, pending: run?.proposals.size, working });
    viewing();
    if (working === typing.isConnected) return;
    const follow = following();
    if (working) log.append(typing); else typing.remove();
    if (follow) log.scrollTop = log.scrollHeight;
  }
  // A card left waiting while nobody looks pushes "Chat needs you" (live._nudge), so the
  // daemon hears each change, and a fresh socket the view at connect, in its handshake.
  function viewing(current = run) {
    const on = dialog.open && !document.hidden;
    if (!current?.listening || current.viewing === on) return;
    current.viewing = on; current.ws.send(JSON.stringify({ action: "viewing", on }));
  }
  const bubble = chatBubble({ licon, open: () => show() });
  const starters = chatStarters($("chat-starters"), sendText);
  const paintStarters = () => starters({ visible: !!run?.text && !run.hasTurn,
    connected: !composing && !!run?.listening && run.ws?.readyState === WebSocket.OPEN });
  // A wide screen (`wide`, app.js's breakpoint) docks the panel beside the work area
  // instead: shown non-modally, so nothing is dimmed or made inert (style.css places it).
  const docked = () => dialog.open && !!wide?.matches;
  // Restoring puts the log back where minimizing left it: at the tail if it was following
  // there, else at the same offset (read before closing, since a closed log has no layout).
  function show() {
    if (!dialog.open) { wide?.matches ? dialog.show() : dialog.showModal(); dialog.focus(); } // not the first button: its ring would show on open
    if (run?.scroll) { log.scrollTop = run.scroll.follow ? log.scrollHeight : run.scroll.top; run.scroll = null; }
    unread = false; badge();
  }
  function minimize() {
    if (run) run.scroll = { top: log.scrollTop, follow: following() };
  }
  const hide = () => { minimize(); dialog.close(); };
  chatComposer($("voice-compose"), {
    session: () => run,
    licon, error: (message) => add("error", message),
    busy: (value) => { composing = value; paintStarters(); },
    send: sendText,
  });
  function sendText(frame, thumbnails = []) {
    if (!run?.listening || run.ws?.readyState !== WebSocket.OPEN) return false;
    run.ws.send(JSON.stringify(frame));
    run.thumbs.push(thumbnails); // for this turn's echo, or its refusal
    run.hasTurn = true; paintStarters(); // hide immediately, including on a double tap
    // Chat only: its daemon answers each queued turn with one turn_complete. A voice model
    // can fold queued typed turns into one reply, so a count there could stick.
    if (run.text) { run.turns++; badge(); }
  }
  async function capabilities() {
    if (fetching) return;
    fetching = true;
    try {
      const data = await request("/api/version");
      onVersion(data.version);
      if (!run) { menu = data.live_models || [{ label: "Default", value: "" }]; renderModels(); }
      // Each button only with something to start: the mic needs a voice row (a keyless one
      // still says what to set), Chat a usable chat model.
      $("live-mode").hidden = !(data.live_enabled && menu.some((model) => !model.text)) && !run;
      $("chat").hidden = !(data.live_enabled && chatModels().length) && !run;
      $("docs").hidden = !data.docs; // not live, but this is the boot capabilities fetch
    } catch { /* Retain the last confirmed capabilities during a tunnel reconnect. */ }
    finally { fetching = false; }
  }
  const saved = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const chatModels = () => menu.filter((model) => model.text && !model.unavailable);
  // The voice picker (a keyless entry greyed, with why), and Chat's switcher options.
  function renderModels() {
    const models = menu.filter((model) => !model.text), last = saved("tmuxrc-live-model");
    const signature = JSON.stringify([menu, last]); // the whole menu: the switcher reads it too
    if (signature === modelSignature) return;
    modelSignature = signature;
    $("voice-switch").replaceChildren(...chatModels().map((model) => Object.assign(document.createElement("option"), { value: model.label, textContent: model.label })));
    $("voice-models").replaceChildren(...models.map((model) => {
      const button = document.createElement("button");
      const image = document.createElement("img"); image.alt = "";
      image.src = /gemini/i.test(model.label) ? "/gemini.svg" : /gpt|openai/i.test(model.label) ? "/openai.svg" : "/icon.svg";
      const label = document.createElement("span"), title = document.createElement("strong"), hint = document.createElement("small");
      title.textContent = model.label; hint.textContent = [model.hint, (model.value ?? model.label) === last ? "Last used" : ""].filter(Boolean).join(" / ");
      label.append(title, hint); button.append(image, label);
      button.insertAdjacentHTML("beforeend", mic);
      button.disabled = !!model.unavailable;
      button.onclick = () => { $("voice-model").value = model.value ?? model.label; start(); };
      return button;
    }));
  }
  function add(role, message, newSegment = false, images = []) {
    const previous = typing.isConnected ? typing.previousElementSibling : log.lastElementChild;
    const follow = following();
    const grow = !newSegment && (role === "user" || role === "model") && previous?.dataset.role === role && !previous.dataset.done;
    let row = previous;
    if (!grow) {
      row = document.createElement("div"); row.className = "voice-entry";
      row.dataset.role = role;
      row.classList.add(["user", "model", "typed", "error", "propose"].includes(role) ? role : "model");
      const heading = document.createElement("strong");
      heading.textContent = { user: "You", model: "Assistant", typed: "Sent to terminal", error: "Connection", propose: "Wants to act" }[role] || "Assistant";
      row.append(heading, document.createElement("div")); log.insertBefore(row, typing.isConnected ? typing : null);
    }
    const body = row.querySelector(".voice-open")?.previousElementSibling || row.lastChild; // text over any Open buttons
    if (role === "model") appendChatMarkdown(body, message, () => { if (follow) log.scrollTop = log.scrollHeight; });
    else body.textContent += message || "";
    body.before(...images.map(chatThumb));
    if (!dialog.open && role !== "user") unread = true; // not the user's own echo
    // Oldest first, but never a proposal still waiting on the user: the daemon would wait
    // forever for a Send/Cancel that is no longer on screen.
    for (let old; log.children.length - typing.isConnected > TRANSCRIPT_ROWS && (old = [...log.children].find((r) => r !== typing && !r.querySelector(".voice-actions")));) old.remove();
    if (follow) log.scrollTop = log.scrollHeight;
    badge();
    return row;
  }
  // A pane-changing action in a text session waits for the user: Send runs it, Cancel
  // tells the model the user declined (live._approved), and so does typing a new message
  // instead (superseded: a card never holds the next turn hostage). The card shows a final
  // answer only once the daemon confirms it ("decided"). A dropped connection leaves it open:
  // the daemon keeps it for this chat a while, so a tap waits out the reconnect, hurried
  // along, and goes again on each new connection until answered (a socket can die with it
  // in flight); only a card the daemon no longer has comes back "expired".
  function propose(current, { id, text, image, pane_id, session }) {
    const row = add("propose", text, false, image ? [image] : []), actions = document.createElement("div");
    if (session) row.lastChild.append(sessionMeta(session));
    actions.className = "voice-actions";
    for (const [label, ok] of [["Send", true], ["Cancel", false]]) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = label;
      if (ok) button.className = "primary";
      button.onclick = () => {
        actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        row.firstChild.textContent = "Sending...";
        const frame = { action: "approve", id, ok };
        current.sending.set(id, frame); deliver(current, [frame]); revive(current);
      };
      actions.append(button);
    }
    // Look before approving: opening leaves the card pending, and the bubble brings it back.
    if (pane_id) actions.append(openButton(pane_id, "Open"));
    row.append(actions); current.proposals.set(id, { row, actions }); badge();
    actions.scrollIntoView?.({ block: "nearest" }); // a card waiting on the user is never left clipped
  }
  // A button to a pane, going exactly where its row in the list goes (the caller's
  // navigate). A phone minimizes the sheet on the way, a docked panel stays put.
  function openButton(pane_id, label) {
    const button = document.createElement("button"); button.type = "button"; button.className = "open";
    button.textContent = label; button.insertAdjacentHTML("beforeend", licon("chevron", 16));
    const at = Date.now(); // a button offered moments ago may name a window state lacks yet
    button.onclick = () => { if (!docked()) hide(); open(pane_id, at); };
    return button;
  }
  // open_pane: the button goes under this turn's reply, in the same bubble, whether it lands
  // before the text (the usual order) or after. `auto` (a resume the user just tapped Send
  // on) also takes that path at once.
  function offer({ pane_id, label, auto }) {
    const button = openButton(pane_id, `Open ${label}`), row = add("model", "");
    (row.querySelector(".voice-open") || row.appendChild(Object.assign(document.createElement("div"), { className: "voice-open" }))).append(button);
    if (auto) button.click();
  }
  // Which session a resume card means, past a title several can share (live._approved).
  function sessionMeta({ tool, cwd, last_active, id }) {
    const at = Date.parse(last_active), meta = document.createElement("small");
    const when = at && `${new Date(at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} · ${since({ state_since: at / 1000 })} ago`;
    meta.className = "voice-meta";
    meta.textContent = [[tool, cwd], [when, id]].map((line) => line.filter(Boolean).join(" · ")).filter(Boolean).join("\n");
    return meta;
  }
  function settle(current, id, label) {
    const card = current.proposals.get(id);
    if (!card) return;
    current.sending.delete(id);
    card.row.firstChild.textContent = label; card.actions.remove(); current.proposals.delete(id); badge();
  }
  function deliver(current, frames) {
    if (current.listening && current.ws?.readyState === WebSocket.OPEN) frames.forEach((frame) => current.ws.send(JSON.stringify(frame)));
  }
  // On each connection: the taps not yet answered go (again), and the untapped cards are
  // checked, since a new message may have superseded one with the answer lost in a drop.
  function resync(current) {
    const untapped = [...current.proposals.keys()].filter((id) => !current.sending.has(id));
    deliver(current, [...current.sending.values(), ...(untapped.length ? [{ action: "sync", ids: untapped }] : [])]);
  }
  // Reconnect now rather than at the backoff's end: a tap waits on it, or the phone is back.
  function revive(current = run) {
    if (current?.ws?.readyState === WebSocket.CLOSED) { clearTimeout(current.retry); connect(current); }
  }
  // A dropped connection takes the daemon's queued turns with it; ending the chat, its cards too.
  function drop(current) { current.turns = 0; badge(); }
  function expire(current) {
    [...current.proposals.keys()].forEach((id) => settle(current, id, "Expired"));
    drop(current);
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
    paintStarters();
    if (current.text) return status(current.listening ? "Connected" : current.connectionStatus || "Connecting...");
    const tracks = current.stream?.getAudioTracks() || [];
    const interrupted = current.capture?.state !== "running" ||
      tracks.some((track) => track.muted) || current.output?.paused || current.audioSession?.state === "interrupted";
    if (!interrupted && current.stream) current.resolveReady?.();
    if (!current.listening && !current.stream) return;
    status(interrupted ? "Audio interrupted. Return to the app and tap the microphone to resume." :
      current.muted ? "Microphone muted" : current.listening ? `Listening / ${current.model || "Default"}` : current.connectionStatus || "Connecting...");
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
      expire(current);
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
    // See lm-tap.js for why the tap worklet has to be a same-origin file rather than an inline blob: module.
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
    if (current.text) query.set("mode", "text");
    if (!current.up) query.set("fresh", "1"); // a new chat: drop any cards an offline end left parked
    current.viewing = dialog.open && !document.hidden; query.set("viewing", current.viewing ? "1" : "0");
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live-mode?${query}`); }
    catch { stop(`Could not connect to ${name(current)}.`); return; }
    current.ws = ws; current.thumbs = []; // an echo lost with the old socket never comes
    clearTimeout(current.deadline);
    current.deadline = setTimeout(() => {
      if (run !== current || current.listening) return;
      if (!current.proposals.size) return stop(`${name(current)} connection timed out. Try again.`);
      try { ws.close(); } catch {}
      connect(current);
    }, CONNECT_DEADLINE_MS);
    ws.onmessage = ({ data }) => {
      if (run !== current || current.ws !== ws) return;
      let message; try { message = JSON.parse(data); } catch { return; }
      if (message.type === "status") {
        current.frameMs = message.frame_ms;
        current.up = true; current.listening = message.status === "listening";
        if (!current.listening) drop(current);
        if (current.listening) { clearTimeout(current.deadline); current.tries = 0; viewing(current); resync(current); }
        current.connectionStatus = message.status === "reconnecting" ? "Reconnecting..." : "Connecting...";
        audioStatus(current);
      } else if (message.type === "transcript") add(message.role, message.text, message.new_segment, "images" in message ? current.thumbs.shift() : []);
      else if (message.type === "turn_complete") {
        [...log.children].forEach((row) => { row.dataset.done = "true"; });
        current.turns = Math.max(0, current.turns - 1); badge(); // a queued turn may follow
      }
      else if (message.type === "typed") add("typed", `${message.label} (${message.pane_id})${message.submitted ? "" : " (not submitted)"}: ${message.text}`);
      else if (message.type === "error") {
        // A refused turn gets no answer; any other error ends the session's turns.
        if (message.refused) current.thumbs.shift();
        current.turns = message.refused ? Math.max(0, current.turns - 1) : 0;
        add("error", message.message);
      }
      else if (message.type === "propose") propose(current, message);
      else if (message.type === "open_pane") offer(message);
      else if (message.type === "expired") settle(current, message.id, "Expired");
      else if (message.type === "decided") settle(current, message.id, message.ok ? "Approved" : message.ok === null ? "Cancelled — you sent a new message" : "Declined");
      else if (message.type === "interrupted") silence(current);
      else if (message.type === "audio") { try { playAudio(current, message.data, message.sample_rate); } catch { add("error", "Could not play this audio chunk."); } }
    };
    ws.onclose = (event) => {
      if (run !== current || current.ws !== ws) return;
      clearTimeout(current.deadline); current.listening = false; drop(current);
      const { retry, refusal } = liveClose(event);
      // The daemon keeps a card for half an hour (live.PARKED_SECONDS), and the tunnel takes
      // a minute to come back from its hourly drop: while one waits, keep trying, capped.
      if (retry && current.up && (current.tries < MAX_RECONNECT_TRIES || current.proposals.size)) {
        current.connectionStatus = "Connection lost. Reconnecting...";
        audioStatus(current);
        current.retry = setTimeout(() => connect(current), 1000 * 2 ** Math.min(current.tries++, MAX_RECONNECT_TRIES - 1));
      // A refusal says whether to reload the tab or go set a key; "Try again" names the
      // one action that cannot help.
      } else if (refusal) stop(refusal);
      else stop(event.code === 1000 ? "Session ended" : `${name(current)} disconnected. Try again.`);
    };
  }
  async function start() {
    const token = ++sequence;
    const current = { model: $("voice-model").value, text: mode === "text", proposals: new Map(), sending: new Map(), thumbs: [], turns: 0, nodes: [], queued: new Set(), playAt: 0, tries: 0, up: false, listening: false, muted: false };
    run = current; log.replaceChildren(); status(current.text ? "Connecting..." : "Connecting microphone..."); paint();
    if (current.text) {
      $("voice-switch").value = current.model;
      try { localStorage.setItem(CHAT_MODEL_KEY, current.model); } catch {}
      audioStatus(current); connect(current); return;
    }
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
      audioStatus(current); connect(current);
    } catch (error) {
      if (run !== current) return;
      report("mic", error);
      stop(`${error.name === "NotAllowedError" ? "Microphone access denied. Allow microphone access for this site." : "Live Mode could not start: " + error.message}`);
    }
  }
  $("live-mode").onclick = () => { if (!run) mode = "voice"; paint(); show(); if (run) resumeAudio(run, true); };
  // Chat: straight into a text session on the last chat model used (else the first), no
  // picker and no mic. A conversation already running (either mode) is just brought back.
  $("chat").onclick = () => {
    if (docked()) return hide(); // a docked panel toggles: nothing covers the button
    const models = chatModels(), model = models.find((entry) => entry.label === saved(CHAT_MODEL_KEY)) || models[0];
    if (!run && model) { mode = "text"; $("voice-model").value = model.label; start(); }
    show();
  };
  $("voice-switch").onchange = () => {
    if (!run?.text || $("voice-switch").value === run.model) return;
    $("voice-model").value = $("voice-switch").value; stop(); start();
  };
  $("voice-close").onclick = hide;
  dialog.addEventListener("cancel", minimize); // Escape, modal only
  // Docked, Escape is no close request, so take it here: only from inside the panel.
  dialog.addEventListener("keydown", (event) => { if (event.key === "Escape" && docked()) hide(); });
  // Crossing the breakpoint reopens the same panel the other way: same session and log.
  wide?.addEventListener?.("change", () => { if (dialog.open) { hide(); show(); } });
  dialog.addEventListener("close", badge);
  // The X beside minimize: end the chat AND dismiss the sheet, so nothing is left to close.
  // (Voice keeps End Live Mode: its sheet stays up for the model picker to start again.)
  $("voice-end").onclick = () => { stop(); dialog.close(); };
  $("voice-start").onclick = () => run ? stop() : start();
  $("voice-mute").onclick = () => {
    if (!run?.stream) return;
    run.muted = !run.muted;
    run.clearPending?.();
    run.stream.getAudioTracks().forEach((track) => { track.enabled = !run.muted; });
    paint(); audioStatus(run); resumeAudio(run, true);
  };
  window.addEventListener("pagehide", () => stop());
  window.addEventListener("online", () => { capabilities(); revive(); });
  // Switching apps is visibilitychange, not navigation: keep the microphone and
  // socket alive. pagehide still releases capture when leaving this document.
  window.addEventListener("pageshow", () => { if (run) { resumeAudio(run); keepAwake(run); } });
  document.addEventListener("visibilitychange", () => {
    if (run) { resumeAudio(run); keepAwake(run); viewing(); }
    if (!document.hidden) { capabilities(); revive(); }
  });
  navigator.serviceWorker?.addEventListener("message", ({ data }) => { if (data === "chat" && run) show(); });
  paint(); capabilities();
  return { isActive: () => !!run, refresh: capabilities };
}
