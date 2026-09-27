const BELL = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 21h3.4M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/></svg>';

function decodeKey(value) {
  const padded = value + "=".repeat((4 - value.length % 4) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

export const pushClientId = (() => {
  try {
    let value = sessionStorage.getItem("tmuxrc-client-id");
    if (!value) {
      value = crypto.randomUUID();
      sessionStorage.setItem("tmuxrc-client-id", value);
    }
    return value;
  } catch {
    try { return crypto.randomUUID(); } catch { return ""; }
  }
})();

export function stateUrl(version = null) {
  const params = new URLSearchParams();
  if (version !== null) params.set("v", version);
  if (pushClientId) {
    params.set("client", pushClientId);
    params.set("visible", document.hidden ? "false" : "true");
  }
  return `/api/state?${params}`;
}

export function sendPresence() {
  if (!pushClientId || !navigator.sendBeacon) return;
  const body = new Blob([JSON.stringify({ client: pushClientId, visible: !document.hidden })],
    { type: "application/json" });
  navigator.sendBeacon("/api/push/presence", body);
}

export async function setupPush(button, announce = () => {}) {
  if (!button || !("serviceWorker" in navigator) || !("PushManager" in window)
      || !("Notification" in window)) return;
  button.hidden = false;
  button.innerHTML = BELL;
  let registration;
  try {
    registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch {
    button.title = "Notifications unavailable";
    button.disabled = true;
    return;
  }
  const refresh = async () => {
    const subscription = await registration.pushManager.getSubscription();
    const enabled = Notification.permission === "granted" && !!subscription;
    button.setAttribute("aria-pressed", String(enabled));
    button.title = enabled ? "Turn off notifications" : "Notify me when a session needs me";
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("active", enabled);
    return subscription;
  };
  const existing = await refresh();
  // The daemon may have restarted or restored an older state file. Re-posting the
  // browser's durable subscription is idempotent and repairs that server-side gap.
  if (existing && Notification.permission === "granted") {
    fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(existing.toJSON()),
    }).catch(() => {});
  }
  button.onclick = async () => {
    button.disabled = true;
    try {
      const current = await registration.pushManager.getSubscription();
      if (current) {
        const response = await fetch("/api/push/unsubscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: current.endpoint }),
        });
        if (!response.ok) throw new Error("Could not turn off notifications");
        await current.unsubscribe();
        announce("Notifications turned off");
      } else {
        const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
        const standalone = navigator.standalone
          || matchMedia("(display-mode: standalone)").matches;
        if (ios && !standalone) {
          throw new Error("On iPhone, first Add to Home Screen and open the installed app");
        }
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted");
        const configResponse = await fetch("/api/push/config");
        if (!configResponse.ok) throw new Error("Could not load notification settings");
        const config = await configResponse.json();
        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: decodeKey(config.public_key),
        });
        const response = await fetch("/api/push/subscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(subscription.toJSON()),
        });
        if (!response.ok) {
          await subscription.unsubscribe();
          throw new Error("Could not save the notification subscription");
        }
        announce("Notifications enabled");
      }
    } catch (error) {
      announce(error.message || "Could not change notifications");
    } finally {
      button.disabled = false;
      await refresh();
    }
  };
}
