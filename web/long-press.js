// A long press: one finger held 500ms, not scrolling. `armed(target)` runs when the hold
// arms, `released(target)` when that finger lifts (where iOS grants a clipboard write), and
// the click it ends in is swallowed, wherever it lands (what the hold opened, a bar, may be
// under the finger by then), before any other listener sees it. Scrolling, a second
// finger or a cancel disarms it. `target` is what the finger went down on, so `el` may be a
// list that delegates for its rows. A classic script (the sandboxed scratch gallery can load
// no module from the app's origin), so it is `longPress` on the global; app.js imports it for that.
globalThis.longPress = (el, { armed = () => {}, released = () => {} } = {}) => {
  let timer, held = false, target;
  const disarm = () => { clearTimeout(timer); held = false; };
  el.addEventListener("touchstart", (event) => {
    disarm();
    if (event.touches.length > 1) return;
    target = event.target;
    timer = setTimeout(() => { held = true; armed(target); }, 500);
    document.addEventListener("touchstart", disarm, { capture: true, once: true, passive: true }); // the next finger anywhere (this one's capture has passed)
  }, { passive: true });
  for (const type of ["touchmove", "touchcancel"]) el.addEventListener(type, disarm, { passive: true });
  el.addEventListener("touchend", () => {
    clearTimeout(timer);
    if (!held) return;
    released(target);
    const swallow = (event) => { event.preventDefault(); event.stopImmediatePropagation(); };
    addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => removeEventListener("click", swallow, true), 400); // a browser that sends none
  }, { passive: true });
  el.addEventListener("contextmenu", (event) => { if (held || event.pointerType === "touch") event.preventDefault(); }); // Android's link menu
};
