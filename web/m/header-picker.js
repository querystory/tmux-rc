// Keep header menus inside the page: native OS select popups can be misplaced
// when Chrome's device emulation scales/offsets the viewport.
export function headerPicker(select) {
  const details = document.createElement('details');
  details.className = 'header-picker';
  details.id = `${select.id}-picker`;
  const summary = document.createElement('summary');
  const caption = document.createElement('span');
  summary.append(caption);
  const menu = document.createElement('div');
  menu.className = 'header-picker-menu';
  menu.setAttribute('role', 'radiogroup');
  menu.setAttribute('aria-label', select.getAttribute('aria-label'));
  details.append(summary, menu);
  select.after(details);
  select.hidden = true;
  let signature;
  function refresh() {
    const options = Array.from(select.options);
    const next = JSON.stringify(options.map(o => [o.value, o.textContent]));
    if (signature !== next) {
      signature = next;
      details.open = false;
      menu.replaceChildren(...options.map(option => {
        const label = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'radio'; input.name = `${select.id}-choice`; input.value = option.value;
        label.append(input, document.createTextNode(option.textContent));
        return label;
      }));
    }
    caption.textContent = options.find(o => o.value === select.value)?.textContent || '';
    summary.setAttribute('aria-label', `${select.getAttribute('aria-label')}: ${caption.textContent}`);
    menu.querySelectorAll('input').forEach(input => { input.checked = input.value === select.value; });
  }
  menu.addEventListener('change', event => {
    select.value = event.target.value;
    details.open = false;
    summary.focus();
    select.dispatchEvent(new Event('change', { bubbles: true }));
    refresh();
  });
  details.addEventListener('keydown', event => {
    if (event.key === 'Escape') { details.open = false; summary.focus(); event.preventDefault(); }
  });
  document.addEventListener('click', event => { if (!details.contains(event.target)) details.open = false; });
  window.addEventListener('resize', () => { details.open = false; });
  select.addEventListener('change', refresh);
  refresh();
  return refresh;
}
