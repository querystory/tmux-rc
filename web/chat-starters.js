// Read-only questions, not shortcuts that authorize a pane-changing action.
export const CHAT_STARTERS = [
  ["What needs attention?", "What needs my attention across all windows? Prioritize anything blocked on me."],
  ["Summarize all windows", "What's going on in all my windows? Give me a concise overview."],
  ["Suggest next actions", "Suggest the next best actions across my windows. Explain why; don't take any actions yet."],
];

export function chatStarters(container, send) {
  const buttons = CHAT_STARTERS.map(([label, prompt]) => {
    const button = document.createElement("button");
    button.type = "button"; button.textContent = label; button.title = prompt;
    button.onclick = () => { if (!button.disabled) send({ action: "text", text: prompt, images: [] }); };
    container.append(button);
    return button;
  });
  return ({ visible, connected }) => {
    container.hidden = !visible;
    buttons.forEach((button) => { button.disabled = !visible || !connected; });
  };
}
