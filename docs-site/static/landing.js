// Local tab navigation and clipboard controls; the website never connects to a daemon.
const themeToggle = document.getElementById('theme-toggle');
function updateThemeControl() {
  const dark = document.documentElement.dataset.theme === 'dark';
  themeToggle.setAttribute('aria-pressed', String(dark));
  themeToggle.title = `Switch to ${dark ? 'light' : 'dark'} mode`; // its name stays "Dark mode": aria-pressed says which
  document.querySelector('meta[name="theme-color"]').content = dark ? '#101312' : '#fafcfb';
}
themeToggle.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('color-theme', theme); } catch {}
  updateThemeControl();
});
updateThemeControl();

for (const group of document.querySelectorAll('[data-tabs]')) {
  const tabs = [...group.querySelectorAll('[role="tab"]')];
  function select(tab) {
    for (const other of tabs) {
      const active = other === tab;
      other.setAttribute('aria-selected', String(active));
      other.tabIndex = active ? 0 : -1;
      const panel = document.getElementById(other.dataset.panel);
      panel.hidden = !active;
      if (active) panel.querySelector('source[data-defer]')?.remove(); // its real image, now wanted
    }
  }
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
      if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length];
      if (event.key === 'Home') next = tabs[0];
      if (event.key === 'End') next = tabs.at(-1);
      if (!next) return;
      event.preventDefault();
      select(next);
      next.focus();
    });
  }
}
if (matchMedia('(max-width:700px)').matches) document.getElementById('phone-tab').click();

for (const button of document.querySelectorAll('[data-copy]')) {
  const label = button.getAttribute('aria-label');
  button.addEventListener('click', async () => {
    const code = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = 'Copied';
      button.setAttribute('aria-label', label.replace(/^Copy /, 'Copied '));
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Select & copy';
      button.setAttribute('aria-label', 'Copy failed. Command selected; copy it manually.');
    }
    setTimeout(() => { button.textContent = 'Copy'; button.setAttribute('aria-label', label); }, 2000);
  });
}

// Any screenshot opens full size; a click anywhere, Esc or the close button shuts it. `src`,
// not `currentSrc`: the hero's phone placeholder must never be what opens.
const zoom = document.getElementById('zoom'), zoomed = zoom.querySelector('img');
for (const img of document.querySelectorAll('.screen img, .phones img, .preview-pair img, .feature-detail img')) {
  img.tabIndex = 0;
  img.setAttribute('role', 'button');
  img.setAttribute('aria-haspopup', 'dialog');
  const open = () => { Object.assign(zoomed, { src: img.src, alt: img.alt }); zoom.showModal(); };
  img.addEventListener('click', open);
  img.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
}
zoom.addEventListener('click', () => zoom.close());
