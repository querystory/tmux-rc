// Keep the same ordered text/image model as the full UI composer.
export class Composer {
  constructor(changed, error) {
    this.changed = changed;
    this.error = error;
    this.files = new Map();
    this.editor = document.createElement("div");
    this.editor.id = "reply";
    this.editor.contentEditable = "true";
    this.editor.setAttribute("role", "textbox");
    this.editor.setAttribute("aria-multiline", "true");
    this.editor.setAttribute("aria-label", "Message this pane");
    this.editor.dataset.placeholder = "Message this pane...";
    this.editor.oninput = () => this.edited();
    this.editor.onpaste = (event) => {
      event.preventDefault();
      if (this.editor.contentEditable !== "true") return;
      const files = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith("image/"));
      if (files.length) files.forEach((file) => this.attach(file));
      else this.insert(document.createTextNode(event.clipboardData?.getData("text/plain") || ""));
    };
    this.editor.ondrop = this.editor.ondragover = (event) => event.preventDefault();
  }
  edited() {
    for (const [chip] of this.files) {
      if (!this.editor.contains(chip)) { URL.revokeObjectURL(chip.src); this.files.delete(chip); }
    }
    if (!this.editor.textContent && !this.files.size) this.editor.replaceChildren();
    this.changed();
  }
  saveCaret() {
    const selection = window.getSelection();
    this.caret = selection?.rangeCount && this.editor.contains(selection.anchorNode)
      ? selection.getRangeAt(0).cloneRange() : null;
  }
  insert(node) {
    this.editor.focus({ preventScroll: true });
    const selection = window.getSelection();
    let range = this.caret;
    if (!range || !this.editor.contains(range.startContainer)) {
      range = selection?.rangeCount && this.editor.contains(selection.anchorNode) ? selection.getRangeAt(0) : null;
    }
    if (!range) { range = document.createRange(); range.selectNodeContents(this.editor); range.collapse(false); }
    this.caret = null;
    range.deleteContents(); range.insertNode(node); range.setStartAfter(node); range.collapse(true);
    selection?.removeAllRanges(); selection?.addRange(range);
    this.edited();
  }
  attach(file) {
    if (!file || this.editor.contentEditable !== "true") return;
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type) || file.size > 20 * 1024 * 1024) {
      this.error("Choose a PNG, JPEG, WebP, or GIF under 20 MB."); return;
    }
    const chip = document.createElement("img");
    chip.className = "attach-chip"; chip.contentEditable = "false"; chip.draggable = false;
    chip.src = URL.createObjectURL(file); chip.alt = file.name || "Image";
    chip.tabIndex = 0; chip.setAttribute("role", "button");
    chip.setAttribute("aria-label", `Remove ${chip.alt}`); chip.title = `Remove ${chip.alt}`;
    const remove = () => {
      if (this.editor.contentEditable !== "true") return;
      chip.remove(); this.editor.focus({ preventScroll: true }); this.edited();
    };
    chip.onclick = remove;
    chip.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); remove(); }
    };
    this.files.set(chip, file); this.insert(chip);
  }
  segments() {
    const segments = [];
    let text = "", started = false, pending = 0;
    const flush = () => { if (text) { segments.push({ text }); text = ""; } };
    const content = () => { text += "\n".repeat(pending); pending = 0; started = true; };
    // Match the full UI's handling of browser-inserted blocks and filler BRs.
    const walk = (node) => {
      [...node.childNodes].forEach((child, index, children) => {
        if (child.nodeType === Node.TEXT_NODE && child.nodeValue) { content(); text += child.nodeValue; }
        else if (this.files.has(child)) { content(); flush(); segments.push({ file: this.files.get(child), chip: child }); }
        else if (child.nodeName === "BR") {
          const filler = node !== this.editor && node.nodeName === "DIV" && index === children.length - 1;
          if (started && !filler) pending++;
        } else { if (child.nodeName === "DIV" && started) pending++; walk(child); }
      });
    };
    walk(this.editor); text += "\n".repeat(pending); flush();
    return segments;
  }
  replace(segments) {
    this.editor.replaceChildren(...segments.map((segment) => segment.chip || document.createTextNode(segment.text)));
    this.edited();
  }
}
