import MarkdownIt from "./m/vendor/markdown-it.mjs";

// Agent replies are untrusted: no HTML, remote images, or non-web link schemes.
const markdown = new MarkdownIt({ html: false, linkify: true });
markdown.validateLink = (url) => /^https?:\/\//i.test(url);
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};

// Pane names in bold: the chat names panes in plain prose ("Pressing Enter in fable session
// monitor"), where they read as words of the sentence. Bolding the names the fleet actually
// has, as whole words, finds them without asking the model to mark them up. `html` is
// already escaped; only the text between tags is touched, and never an entity (`&amp;`).
const pattern = (text) => markdown.utils.escapeHtml(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function markNames(html, names) {
  const alternatives = [...new Set(names.filter(Boolean))].sort((a, b) => b.length - a.length).map(pattern);
  if (!alternatives.length) return html;
  const name = new RegExp(`(?<![\\p{L}\\p{N}&])(?:${alternatives.join("|")})(?![\\p{L}\\p{N}])`, "giu");
  return html.split(/(<[^>]*>)/).map((part, i) => i % 2 ? part : part.replace(name, "<strong>$&</strong>")).join("");
}
export const renderChatMarkdown = (text, names = []) => markNames(markdown.render(text), names);
// A line the daemon wrote (a card, what was typed): no Markdown, the same bold names.
export const renderChatText = (text, names = []) => markNames(markdown.utils.escapeHtml(text), names);
const sources = new WeakMap();

// Reparse the whole source: a closing delimiter may arrive later. Coalesce rapid
// deltas to one render per frame rather than doing full parsing for every token.
// Reading textContent back would lose Markdown delimiters after the first render.
export function appendChatMarkdown(element, chunk, rendered = () => {}, names = []) {
  let state = sources.get(element);
  if (!state) { state = { source: "", pending: false }; sources.set(element, state); }
  state.source += chunk || ""; state.rendered = rendered; state.names = names;
  if (state.pending) return;
  state.pending = true;
  requestAnimationFrame(() => {
    state.pending = false;
    if (element.isConnected === false) return;
    element.classList.add("chat-markdown");
    element.innerHTML = renderChatMarkdown(state.source, state.names);
    state.rendered();
  });
}
