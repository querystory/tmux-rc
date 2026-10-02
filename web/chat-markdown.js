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

// Reparse the whole source on each chunk: a closing delimiter may arrive later.
// Reading textContent back would lose Markdown delimiters after the first render.
export function appendChatMarkdown(element, chunk) {
  const source = (sources.get(element) || "") + (chunk || "");
  sources.set(element, source);
  element.classList.add("chat-markdown");
  element.innerHTML = renderChatMarkdown(source);
}
