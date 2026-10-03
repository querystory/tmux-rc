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

export const renderChatMarkdown = (text) => markdown.render(text);
const sources = new WeakMap();

// Reparse the whole source: a closing delimiter may arrive later. Coalesce rapid
// deltas to one render per frame rather than doing full parsing for every token.
// Reading textContent back would lose Markdown delimiters after the first render.
export function appendChatMarkdown(element, chunk, rendered = () => {}) {
  let state = sources.get(element);
  if (!state) { state = { source: "", pending: false }; sources.set(element, state); }
  state.source += chunk || ""; state.rendered = rendered;
  if (state.pending) return;
  state.pending = true;
  requestAnimationFrame(() => {
    state.pending = false;
    if (element.isConnected === false) return;
    element.classList.add("chat-markdown");
    element.innerHTML = renderChatMarkdown(state.source);
    state.rendered();
  });
}
