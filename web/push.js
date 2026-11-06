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

// The bell's one state, from the browser's permission and subscription and the endpoint the daemon acked.
const pushState = (permission, subscription, saved) => !subscription ? "Off"
  : permission !== "granted" ? "Orphaned" : saved === subscription.endpoint ? "On" : "NeedsRepair";
const PUSH_LABELS = {
  Off: "Notify me when a session needs me",
  On: "Turn off notifications",
  NeedsRepair: "Reconnect notifications",
  Orphaned: "Notify me when a session needs me", // permission revoked in the OS: it reads, and acts, as Off
};

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
  let subscription, saved = null; // saved: the endpoint the daemon last acknowledged
  const state = () => pushState(Notification.permission, subscription, saved);
  const save = async () => {
    const { endpoint } = subscription; // a visibility refresh may replace it while this is out
    const response = await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON()),
    }).catch(() => null);
    saved = response?.ok ? endpoint : null;
    return !!saved;
  };
  const refresh = async () => {
    subscription = await registration.pushManager.getSubscription();
    const on = state() === "On";
    button.setAttribute("aria-pressed", String(on));
    button.title = PUSH_LABELS[state()];
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("active", on);
  };
  subscription = await registration.pushManager.getSubscription();
  // The daemon may have restarted or restored an older state file. Re-posting the
  // browser's durable subscription is idempotent and repairs that server-side gap.
  if (state() === "NeedsRepair" && !await save()) {
    announce("Notifications could not reconnect; tap Push notifications in Settings to reconnect");
  }
  await refresh();
  // Permission is changed in the OS's settings, outside the app: re-read on the way back.
  document.addEventListener("visibilitychange", () => document.hidden || refresh());
  button.disabled = false;
  button.onclick = async () => {
    button.disabled = true;
    try {
      if (state() === "On") {
        const response = await fetch("/api/push/unsubscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        });
        if (!response.ok) throw new Error("Could not turn off notifications");
        await subscription.unsubscribe();
        announce("Notifications turned off");
      } else { // Off, NeedsRepair and Orphaned all turn on: get permission, a subscription, the daemon's ack
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
        // Re-read: a browser may have dropped the held subscription along with the permission.
        subscription = await registration.pushManager.getSubscription();
        if (!subscription) {
          const configResponse = await fetch("/api/push/config");
          if (!configResponse.ok) throw new Error("Could not load notification settings");
          const config = await configResponse.json();
          subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true, applicationServerKey: decodeKey(config.public_key),
          });
        }
        // A subscription the daemon didn't take stays, as NeedsRepair: the next tap or boot re-posts it.
        if (!await save()) throw new Error("Could not turn on notifications; tap to reconnect");
        announce("Notifications turned on");
      }
    } catch (error) {
      announce(error.message || "Could not change notifications");
    } finally {
      button.disabled = false;
      await refresh();
    }
  };
}
