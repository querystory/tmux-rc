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

// The screenshot strip: a chip jumps to its feature's first shot, the toggle filters by device,
// and the chip of the leftmost shot in view lights up. A jump pins its chip until the visitor
// scrolls, since the strip's last shots can never reach its left edge.
const strip = document.getElementById('strip'), shots = [...strip.children];
const chips = [...document.querySelectorAll('[data-feature]:not(.shot)')], filters = [...document.querySelectorAll('[data-filter]')];
const press = (buttons, on) => { for (const button of buttons) button.setAttribute('aria-pressed', String(button === on)); };
const highlight = (feature) => press(chips, chips.find((chip) => chip.dataset.feature === feature));
const inView = new Set();
let pinned = false;
const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) entry.isIntersecting ? inView.add(entry.target) : inView.delete(entry.target);
  const first = shots.find((shot) => inView.has(shot));
  if (first && !pinned) highlight(first.dataset.feature);
}, { root: strip, threshold: 0.6 });
for (const shot of shots) observer.observe(shot);
function filter(show) {
  strip.dataset.show = show;
  press(filters, filters.find((button) => button.dataset.filter === show));
  strip.scrollLeft = 0;
  ends();
}
for (const button of filters) button.addEventListener('click', () => { pinned = false; filter(button.dataset.filter); });
for (const chip of chips) chip.addEventListener('click', () => {
  const first = () => shots.find((shot) => shot.dataset.feature === chip.dataset.feature && shot.offsetParent);
  if (!first()) filter('all'); // the feature has no shot on this device
  pinned = true;
  highlight(chip.dataset.feature);
  first().scrollIntoView({ block: 'nearest', inline: 'start' }); // smooth unless reduced motion (landing.css)
});
for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) strip.addEventListener(type, () => { pinned = false; }, { passive: true });
const [back, forward] = document.querySelectorAll('[data-step]');
for (const button of [back, forward]) button.addEventListener('click', () => { pinned = false; strip.scrollBy({ left: button.dataset.step * strip.clientWidth * 0.8 }); });
function ends() {
  back.disabled = strip.scrollLeft < 1;
  forward.disabled = strip.scrollLeft + strip.clientWidth > strip.scrollWidth - 1;
}
strip.addEventListener('scroll', ends, { passive: true });
addEventListener('resize', ends);
ends();

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

// Any screenshot opens full size; a click anywhere, Esc or the close button shuts it.
const zoom = document.getElementById('zoom'), zoomed = zoom.querySelector('img');
for (const img of document.querySelectorAll('.shot img, .feature-detail img')) {
  img.tabIndex = 0;
  img.setAttribute('role', 'button');
  img.setAttribute('aria-haspopup', 'dialog');
  const open = () => { Object.assign(zoomed, { src: img.src, alt: img.alt }); zoom.showModal(); };
  img.addEventListener('click', open);
  img.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
}
zoom.addEventListener('click', () => zoom.close());
