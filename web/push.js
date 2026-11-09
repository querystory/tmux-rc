function decodeKey(value) {
  const padded = value + "=".repeat((4 - value.length % 4) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
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
  const save = async (held) => {
    const response = await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(held.toJSON()),
    }).catch(() => null);
    saved = response?.ok ? held.endpoint : null;
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
  if (state() === "NeedsRepair" && !await save(subscription)) {
    announce("Notifications could not reconnect; tap Push notifications in Settings to reconnect");
  }
  await refresh();
  // Permission is changed in the OS's settings, outside the app: re-read on the way back,
  // unless a tap is under way (it refreshes after).
  document.addEventListener("visibilitychange", () => document.hidden || button.disabled || refresh());
  button.disabled = false;
  button.onclick = async () => {
    button.disabled = true;
    let held = subscription; // the tap acts on its own copy: a refresh may land meanwhile
    try {
      if (state() === "On") {
        const response = await fetch("/api/push/unsubscribe", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: held.endpoint }),
        });
        if (!response.ok) throw new Error("Could not turn off notifications");
        saved = null; // the daemon has dropped it, even if the browser fails to below
        await held.unsubscribe();
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
        held = await registration.pushManager.getSubscription();
        if (!held) {
          const configResponse = await fetch("/api/push/config");
          if (!configResponse.ok) throw new Error("Could not load notification settings");
          const config = await configResponse.json();
          held = await registration.pushManager.subscribe({
            userVisibleOnly: true, applicationServerKey: decodeKey(config.public_key),
          });
        }
        // A subscription the daemon didn't take stays, as NeedsRepair: the next tap or boot re-posts it.
        if (!await save(held)) throw new Error("Could not turn on notifications; tap to reconnect");
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
