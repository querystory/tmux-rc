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

// Inside the native shell (docs/design/native-app.md) the page has no service worker: the
// shell's push plugin stands in for the browser's Notification and PushManager, and a tap
// on a notification comes here to be opened, with its chat card if it names one. The shell's token reads "<APNs environment>:<hex>".
// The device's token outlives the switch, so `store` holds whether it is on.
export function nativePush(plugin, store, open) {
  const waiting = [];
  const settle = (side) => (value) => waiting.splice(0).forEach((pair) => pair[side](value));
  plugin.addListener("registration", ({ value }) => settle(0)(value));
  plugin.addListener("registrationError", ({ error }) => settle(1)(new Error(error)));
  plugin.addListener("pushNotificationActionPerformed", ({ actionId, notification }) => {
    const to = new URL(notification.data?.url || "/m", location.origin);
    if (actionId === "tap" && to.origin === location.origin) open(to.href, notification.data?.chat);
  });
  const device = async () => {
    const value = await new Promise((ok, fail) => { waiting.push([ok, fail]); plugin.register(); });
    const [env, token] = value.split(":"), id = { kind: "apns", token };
    return { endpoint: token, toJSON: () => ({ ...id, env }), off: id, unsubscribe: async () => store.removeItem("tmuxrc-native-push") };
  };
  const native = {
    permission: "default",
    requestPermission: async () => (native.permission = (await plugin.requestPermissions()).receive),
    getSubscription: async () => {
      native.permission = (await plugin.checkPermissions()).receive;
      return store.getItem("tmuxrc-native-push") ? device().catch(() => null) : null; // no token now: Off, and a tap asks again
    },
    subscribe: async (config) => { // the daemon sends to the app only when it holds an APNs key
      if (!config.native?.includes("apns")) throw new Error("This daemon is not set up to notify the app");
      const held = await device(); store.setItem("tmuxrc-native-push", "1"); return held;
    },
  };
  return native;
}

const pushOff = (held) => fetch("/api/push/unsubscribe", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(held.off || { endpoint: held.endpoint }),
});

// The shell's "Change server": the daemon being left would keep notifying this device.
export async function leavePush(native) {
  const held = await native.getSubscription();
  if (held) { await pushOff(held).catch(() => {}); await held.unsubscribe(); }
}

export async function setupPush(button, announce = () => {}, bell = "", native = null) {
  if (!button || !native && (!("serviceWorker" in navigator) || !("PushManager" in window)
      || !("Notification" in window))) return;
  button.hidden = false;
  button.innerHTML = bell;
  button.disabled = true;
  let manager = native;
  if (!native) try {
    await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    manager = (await navigator.serviceWorker.ready).pushManager;
  } catch {
    button.title = "Notifications unavailable";
    button.disabled = true;
    return;
  }
  const permissions = native || Notification;
  let subscription, saved = null; // saved: the endpoint the daemon last acknowledged
  const state = () => pushState(permissions.permission, subscription, saved);
  const save = async (held) => {
    const response = await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(held.toJSON()),
    }).catch(() => null);
    saved = response?.ok ? held.endpoint : null;
    // A daemon that refuses the app's token holds no APNs key: the device goes back to Off
    // instead of asking again on every boot.
    if (native && response?.status === 400) await held.unsubscribe();
    return !!saved;
  };
  const refresh = async () => {
    subscription = await manager.getSubscription();
    const on = state() === "On";
    button.setAttribute("aria-pressed", String(on));
    button.title = PUSH_LABELS[state()];
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("active", on);
  };
  subscription = await manager.getSubscription();
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
        const response = await pushOff(held);
        if (!response.ok) throw new Error("Could not turn off notifications");
        saved = null; // the daemon has dropped it, even if the browser fails to below
        await held.unsubscribe();
        announce("Notifications turned off");
      } else { // Off, NeedsRepair and Orphaned all turn on: get permission, a subscription, the daemon's ack
        const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
        const standalone = navigator.standalone
          || matchMedia("(display-mode: standalone)").matches;
        if (!native && ios && !standalone) {
          throw new Error("On iPhone, first Add to Home Screen and open the installed app");
        }
        // This must be the first awaited browser operation in the enable branch: Safari
        // requires requestPermission() to observe the button's transient user activation.
        const permission = permissions.permission === "granted"
          ? "granted" : await permissions.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted");
        // Re-read: a browser may have dropped the held subscription along with the permission.
        held = await manager.getSubscription();
        if (!held) {
          const configResponse = await fetch("/api/push/config");
          if (!configResponse.ok) throw new Error("Could not load notification settings");
          const config = await configResponse.json();
          held = native ? await native.subscribe(config) : await manager.subscribe({
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
