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

export async function setupPush(button, announce = () => {}, bell = "") {
  if (!button || !("serviceWorker" in navigator) || !("PushManager" in window)
      || !("Notification" in window)) return;
  button.hidden = false;
  button.innerHTML = bell;
  button.disabled = true;
  let registration;
  try {
    await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    registration = await navigator.serviceWorker.ready;
  } catch {
    button.title = "Notifications unavailable";
    button.disabled = true;
    return;
  }
  let subscription;
  const refresh = async () => {
    subscription = await registration.pushManager.getSubscription();
    const enabled = Notification.permission === "granted" && !!subscription;
    button.setAttribute("aria-pressed", String(enabled));
    button.title = enabled ? "Turn off notifications" : "Notify me when a session needs me";
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("active", enabled);
  };
  await refresh();
  // The daemon may have restarted or restored an older state file. Re-posting the
  // browser's durable subscription is idempotent and repairs that server-side gap.
  if (subscription && Notification.permission === "granted") {
    const repaired = await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON()),
    }).catch(() => null);
    if (!repaired?.ok) announce("Notifications could not reconnect; tap the bell to retry");
  }
  button.disabled = false;
  button.onclick = async () => {
    button.disabled = true;
    try {
      if (subscription) {
        const response = await fetch("/api/push/unsubscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        });
        if (!response.ok) throw new Error("Could not turn off notifications");
        await subscription.unsubscribe();
        subscription = null;
        announce("Notifications turned off");
      } else {
        const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
        const standalone = navigator.standalone
          || matchMedia("(display-mode: standalone)").matches;
        if (ios && !standalone) {
          throw new Error("On iPhone, first Add to Home Screen and open the installed app");
        }
        // This must be the first awaited browser operation in the enable branch: Safari
        // requires requestPermission() to observe the button's transient user activation.
        const permission = Notification.permission === "granted"
          ? "granted" : await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted");
        const configResponse = await fetch("/api/push/config");
        if (!configResponse.ok) throw new Error("Could not load notification settings");
        const config = await configResponse.json();
        subscription = await registration.pushManager.subscribe({
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
