// Shared by the placeholder and an updating page's bar (scratch._ask). Sandboxed like every
// /scratch/ page, there is no fetch, so it asks by image whether what it shows changed (the page
// landed, the placeholder moved on, the update finished or its files changed): a wider image says
// yes, and only a yes reloads. A failed ask (the tunnel down) never navigates, or an error page
// with no script would be left in its place for good: it says so and asks again, backing off to 30s.
// A wider image says it is done: a hidden tab tells so (tell) and reloads once it is looked at.
const SEEN = "{seen}";
// While the tab is hidden, a notification that brings it back when clicked, one per folder: each
// replaces the last, and its push's Ready (push.py's tag). Only the placeholder may notify: an
// opaque origin never gets the permission.
// Through the app's service worker where there is one (a page's own notification never replaces
// a push's, and Android has no other), whose click focuses this tab (sw.js).
const tell = (title, body) => { if (!document.hidden || window.Notification?.permission !== "granted") return false;
  const as = { body, tag: "scratch:" + decodeURIComponent(location.pathname.split("/").filter(Boolean).pop()),
    renotify: true, data: { url: location.href } };
  Promise.resolve(navigator.serviceWorker?.getRegistration()).then((sw) => {
    if (sw) return sw.showNotification(title, as);
    new Notification(title, as).onclick = (e) => { focus(); e.target.close(); }; });
  return true; };
let fails = 0;
const ask = () => { const img = new Image(); let late; // an ask unanswered in 10s failed too
  const end = (ok) => { if (!late) return; clearTimeout(late); late = null; img.onload = img.onerror = null;
    document.getElementById("offline").hidden = ok || ++fails < 2;
    if (ok) fails = 0;
    if (ok && img.naturalWidth > 2) window.name = ""; // the placeholder's memory, done with
    if (ok && img.naturalWidth > 2 && tell("Ready: " + document.title.replace(/^.*? · /, "")))
      return document.addEventListener("visibilitychange", () => location.reload(), { once: true });
    if (ok && img.naturalWidth > 1 && !document.querySelector("a:hover")) return location.reload();
    setTimeout(ask, ok ? {every} : Math.min({every} * 2 ** fails, 30000)); };
  late = setTimeout(() => { end(false); img.src = ""; }, 10000);
  img.onload = () => end(true); img.onerror = () => end(false);
  img.src = location.pathname.match(/^\/[^/]+\/(\.archive\/)?[^/]+/)[0] + "/?seen=" + SEEN + "&t=" + Date.now(); }; // its folder's
setTimeout(ask, {every});
