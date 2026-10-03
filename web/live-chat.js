// Live Mode's text chat pieces, used by the phone UI (m/live.js):
// the typed-turn composer, the bubble a minimized chat shrinks to, and transcript thumbnails.
// Rationale: docs/design/live-mode.md § Chat entry.
import { Composer, bindAttach, enterSubmits } from "/m/composer.js";

const TYPED_TURN_CHARS = 4000; // live.TYPED_TURN_CHARS: past it the daemon refuses the turn
const CHAT_IMAGES = 4; // live.CHAT_IMAGES: images one turn may carry
// Long edge a pasted image is sent at. Claude downsamples anything larger, and a phone
// photo sent whole is several MB of base64 per turn, past its per-image cap.
const IMAGE_EDGE = 1568;

// A pasted image as the chat model gets it: re-encoded as a JPEG no larger than IMAGE_EDGE.
// The data URL doubles as the transcript thumbnail.
async function imagePart(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  // At least a pixel each way: a 1x4000 strip would otherwise scale to an empty canvas.
  canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  context.fillStyle = "#fff"; context.fillRect(0, 0, canvas.width, canvas.height); // JPEG has no alpha
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close(); // free the decoded photo now, not at the next GC
  const url = canvas.toDataURL("image/jpeg", 0.85);
  return { mime: "image/jpeg", data: url.slice(url.indexOf(",") + 1), url };
}

// Fills `form` with the pane composer's editor (paste and the attach button add images in
// line, with the same types and size limit), an attach button and Send. Enter sends.
// send(frame, thumbnails) gets the socket's text frame and each image's thumbnail URL; it
// returns false (not connected) to keep the draft. session() names the current
// conversation, so a turn is never sent into a different one than it was written in.
export function chatComposer(form, { licon, send, error, session, busy = () => {} }) {
  const composer = new Composer(() => {}, error, { id: "chat-input", label: "Message the assistant", max: CHAT_IMAGES });
  const attach = document.createElement("button"), picker = document.createElement("input");
  const submit = document.createElement("button");
  attach.type = "button"; attach.className = "icon-button chat-attach"; attach.innerHTML = licon("paperclip");
  attach.title = attach.ariaLabel = "Attach image";
  picker.type = "file"; picker.multiple = true; picker.accept = "image/png,image/jpeg,image/webp,image/gif"; picker.hidden = true;
  submit.type = "submit"; submit.className = "primary"; submit.textContent = "Send";
  bindAttach(attach, picker, () => composer);
  enterSubmits(form, (target) => composer.editor.contains(target));
  // One turn at a time. Only a turn with images awaits (the transcode), and for that
  // moment the editor is locked (the Composer ignores paste and attach then), so a second
  // Enter cannot resend it and nothing typed meanwhile is cleared with the sent draft. A
  // plain turn stays synchronous and keeps the editor, and a phone keyboard, up.
  let sending = false;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const segments = composer.segments();
    const text = segments.map((segment) => segment.text || "").join("").trim();
    const files = segments.filter((segment) => segment.file).map((segment) => segment.file);
    if (sending || (!text && !files.length)) return;
    if (text.length > TYPED_TURN_CHARS) return error("Too long; not sent");
    if (files.length > CHAT_IMAGES) return error(`At most ${CHAT_IMAGES} images a turn; not sent`);
    const owner = session(); // the conversation this draft was written in
    let images = [];
    if (files.length) {
      sending = attach.disabled = true; composer.editor.contentEditable = "false";
      busy(true);
      // One at a time: a 20 MB phone photo decodes to far more than that, so decoding
      // several at once can take a phone tab down before any is downscaled.
      try { for (const file of files) images.push(await imagePart(file)); }
      catch { return error("Could not read that image; not sent"); }
      finally { sending = attach.disabled = false; composer.editor.contentEditable = "true"; busy(false); }
    }
    const frame = { action: "text", text, images: images.map(({ mime, data }) => ({ mime, data })) };
    // A model switch (a new session) during the transcode keeps the draft unsent: it was
    // written to the other model, and the new one has none of that conversation.
    if (session() !== owner) return error("The conversation changed; not sent");
    if (send(frame, images.map((image) => image.url)) !== false) composer.replace([]);
  };
  form.append(attach, composer.editor, submit, picker);
}

// A pasted image as it appears on the user's turn in the transcript.
export const chatThumb = (src) => Object.assign(document.createElement("img"), { src, alt: "Pasted image", className: "chat-thumb" });

// A minimized conversation: a floating button that restores it, with a dot for anything
// said while it was down and a count of consent cards still waiting on Send or Cancel. A
// voice session minimizes to the same button under its own name and the mic.
// It pulses while a reply is being worked on.
// Returns paint({shown, voice, unread, pending, working}), with paint.focus() for handing focus over.
export function chatBubble({ licon, open }) {
  const bubble = document.createElement("button"), glyph = document.createElement("span");
  bubble.type = "button"; bubble.className = "chat-bubble"; bubble.hidden = true;
  const badge = document.createElement("span"); badge.className = "chat-badge";
  bubble.append(glyph, badge);
  bubble.onclick = open;
  document.body.append(bubble);
  return Object.assign(({ shown, voice, unread, pending, working }) => {
    bubble.hidden = !shown;
    if (glyph.dataset.voice !== String(!!voice)) { glyph.dataset.voice = !!voice; glyph.innerHTML = licon(voice ? "mic" : "message"); }
    bubble.classList.toggle("unread", !!unread);
    bubble.classList.toggle("working", !!working);
    badge.textContent = pending || "";
    const name = voice ? "Live Mode" : "Chat";
    const states = [pending && `${pending} waiting for you`, unread && "new messages", working && "responding"].filter(Boolean);
    bubble.title = bubble.ariaLabel = `Open ${name}${states.length ? `: ${states.join(", ")}` : ""}`;
  }, { focus: () => bubble.focus() });
}
