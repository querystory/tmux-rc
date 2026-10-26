// Shared by the placeholder and an updating page's bar (scratch._ask). Sandboxed like every
// /scratch/ page, there is no fetch, so it asks by image whether what it shows changed (the page
// landed, the placeholder moved on, the update finished or its files changed): a wider image says
// yes, and only a yes reloads. A failed ask (the tunnel down) never navigates, or an error page
// with no script would be left in its place for good: it says so and asks again, backing off to 30s.
const SEEN = "{seen}";
let fails = 0;
const ask = () => { const img = new Image(); let late; // an ask unanswered in 10s failed too
  const end = (ok) => { if (!late) return; clearTimeout(late); late = null; img.onload = img.onerror = null;
    document.getElementById("offline").hidden = ok || ++fails < 2;
    if (ok) fails = 0;
    if (ok && img.naturalWidth > 1 && !document.querySelector("a:hover")) return location.reload();
    setTimeout(ask, ok ? {every} : Math.min({every} * 2 ** fails, 30000)); };
  late = setTimeout(() => { end(false); img.src = ""; }, 10000);
  img.onload = () => end(true); img.onerror = () => end(false);
  img.src = location.pathname.split("/").slice(0, 3).join("/") + "/?seen=" + SEEN + "&t=" + Date.now(); }; // its folder's
setTimeout(ask, {every});
