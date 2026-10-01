// How a Live Mode socket close ends, used by m/live.js.
// 1000/1005 are clean. 1008 is the server refusing the handshake: there is no socket to
// send an error frame on, so the reason rides on the close, and it is definitive — a
// reconnect refused mid-session (key removed, label no longer offered) gets the same answer.
export const liveClose = ({ code, reason }) => ({
  retry: ![1000, 1005, 1008].includes(code),
  refusal: (code === 1008 && reason) || "",
});
