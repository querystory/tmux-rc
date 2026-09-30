// Live Mode's text chat pieces, shared by the full UI (app.js) and the phone (m/live.js):
// the typed-turn composer, the bubble a minimized chat shrinks to, and transcript thumbnails.
// Rationale: docs/design/live-mode.md § Chat entry.
import { Composer, bindAttach, enterSubmits } from "/m/composer.js";

const TYPED_TURN_CHARS = 4000; // live.TYPED_TURN_CHARS: past it the daemon refuses the turn
// Long edge a pasted image is sent at. Claude downsamples anything larger, and a phone
// photo sent whole is several MB of base64 per turn, past its per-image cap.
const IMAGE_EDGE = 1568;

// A pasted image as the chat model gets it: re-encoded as a JPEG no larger than IMAGE_EDGE.
// The data URL doubles as the transcript thumbnail.
async function imagePart(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, IMAGE_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
  const context = canvas.getContext("2d");
  context.fillStyle = "#fff"; context.fillRect(0, 0, canvas.width, canvas.height); // JPEG has no alpha
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const url = canvas.toDataURL("image/jpeg", 0.85);
  return { mime: "image/jpeg", data: url.slice(url.indexOf(",") + 1), url };
}

// Fills `form` with the pane composer's editor (paste and the attach button add images in
// line, with the same types and size limit), an attach button and Send. Enter sends.
// send(frame, thumbnails) gets the socket's text frame and each image's thumbnail URL; it
// returns false (not connected) to keep the draft.
export function chatComposer(form, { licon, send, error }) {
  const composer = new Composer(() => {}, error, { id: "chat-input", label: "Message the assistant" });
  const attach = document.createElement("button"), picker = document.createElement("input");
  const submit = document.createElement("button");
  attach.type = "button"; attach.className = "icon-button chat-attach"; attach.innerHTML = licon("paperclip");
  attach.title = attach.ariaLabel = "Attach image";
  picker.type = "file"; picker.accept = "image/png,image/jpeg,image/webp,image/gif"; picker.hidden = true;
  submit.type = "submit"; submit.className = "primary"; submit.textContent = "Send";
  bindAttach(attach, picker, () => composer);
  enterSubmits(form, (target) => composer.editor.contains(target));
  form.onsubmit = async (event) => {
    event.preventDefault();
    const segments = composer.segments();
    const text = segments.map((segment) => segment.text || "").join("").trim();
    const files = segments.filter((segment) => segment.file).map((segment) => segment.file);
    if (!text && !files.length) return;
    if (text.length > TYPED_TURN_CHARS) return error("Too long; not sent");
    let images;
    try { images = await Promise.all(files.map(imagePart)); } catch { return error("Could not read that image; not sent"); }
    const frame = { action: "text", text, images: images.map(({ mime, data }) => ({ mime, data })) };
    if (send(frame, images.map((image) => image.url)) !== false) composer.replace([]);
  };
  form.append(attach, composer.editor, submit, picker);
}

// A pasted image as it appears on the user's turn in the transcript.
export const chatThumb = (src) => Object.assign(document.createElement("img"), { src, alt: "Pasted image", className: "chat-thumb" });

// A minimized conversation: a floating button that restores it, with a dot for anything
// said while it was down and a count of consent cards still waiting on Send or Cancel. A
// voice session minimizes to the same button under its own name and the mic.
// Returns paint({shown, voice, unread, pending}).
export function chatBubble({ licon, open }) {
  const bubble = document.createElement("button"), glyph = document.createElement("span");
  bubble.type = "button"; bubble.className = "chat-bubble"; bubble.hidden = true;
  const badge = document.createElement("span"); badge.className = "chat-badge";
  bubble.append(glyph, badge);
  bubble.onclick = open;
  document.body.append(bubble);
  return ({ shown, voice, unread, pending }) => {
    bubble.hidden = !shown;
    if (glyph.dataset.voice !== String(!!voice)) { glyph.dataset.voice = !!voice; glyph.innerHTML = licon(voice ? "mic" : "message"); }
    bubble.classList.toggle("unread", !!unread);
    badge.textContent = pending || "";
    const name = voice ? "Live Mode" : "Chat";
    bubble.title = bubble.ariaLabel = `Open ${name}${pending ? `: ${pending} waiting for you` : unread ? ": new messages" : ""}`;
  };
}
