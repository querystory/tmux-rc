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
  if (select.hasAttribute('aria-description')) menu.setAttribute('aria-description', select.getAttribute('aria-description'));
  details.append(summary, menu);
  select.after(details);
  select.hidden = true;
  let signature;
  function refresh() {
    const options = Array.from(select.options);
    const next = JSON.stringify(options.map(o => [o.value, o.textContent, o.title, o.dataset.thumb]));
    if (signature !== next) {
      signature = next;
      details.open = false;
      menu.replaceChildren(...options.map(option => {
        const label = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'radio'; input.name = `${select.id}-choice`; input.value = option.value;
        // An option's title, when it has one, is its one-line description under the name.
        const name = document.createElement('span');
        name.textContent = option.textContent;
        if (option.title) name.append(Object.assign(document.createElement('small'), { textContent: option.title }));
        label.append(input, name);
        // And its data-thumb, markup drawn at the end of the row (a picture of the choice).
        if (option.dataset.thumb) label.append(Object.assign(document.createElement('span'), { className: 'thumb', ariaHidden: 'true', innerHTML: option.dataset.thumb }));
        return label;
      }));
    }
    caption.textContent = options.find(o => o.value === select.value)?.textContent || '';
    summary.title = `${select.getAttribute('aria-label')}: ${caption.textContent}`;
    summary.setAttribute('aria-label', summary.title);
    menu.querySelectorAll('input').forEach(input => { input.checked = input.value === select.value; });
  }
  menu.addEventListener('change', event => {
    select.value = event.target.value;
    details.open = false;
    summary.focus();
    select.dispatchEvent(new Event('change', { bubbles: true }));
    refresh();
  });
  dismissable(details);
  select.addEventListener('change', refresh);
  refresh();
  return refresh;
}

// Escape, a click outside, or a resize closes a <details> menu — shared by the pickers
// and the pane's overflow menu so there is one menu behavior, not two.
export function dismissable(details) {
  const summary = details.querySelector('summary');
  details.addEventListener('keydown', event => {
    if (event.key === 'Escape') { details.open = false; summary.focus(); event.preventDefault(); }
  });
  // The path as dispatched: a click that redraws its own button (a multi-select's) is still inside.
  document.addEventListener('click', event => { if (!event.composedPath().includes(details)) details.open = false; });
  window.addEventListener('resize', () => { details.open = false; });
  // A tap in a frame (the Scratch gallery) never reaches this document, but it takes the focus.
  window.addEventListener('blur', () => { details.open = false; });
}
