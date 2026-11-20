// /reauth (#818): the console's "Re-sign in" opens this page in a pop-up (else a new tab). It is behind Cloudflare
// Access like every page, so reaching it means the sign-in is fresh. It tells the console, to this origin only, in
// three ways, none of which needs the opener (Access's login page, a browser or a plain new tab may have cut it, #826):
//   BroadcastChannel "sizeup-session"   every page of this origin (a channel is never shared with another origin)
//   localStorage "sizeup:signed-in"     the storage event, in this origin's other pages (no BroadcastChannel)
//   postMessage to the opener           targetOrigin = this origin: never another site's window
// The message carries nothing: the console asks GET /api/session itself, and closes this pop-up. It also closes
// itself; a window that can't be closed (a plain tab) says "Signed in" with a Close button.
"use strict";

(() => {
  const MSG = { type: "sizeup:signed-in" };
  try {
    const ch = new BroadcastChannel("sizeup-session");
    ch.postMessage(MSG);
    ch.close();
  } catch (_) { /* no BroadcastChannel: the storage event below */ }
  try {
    localStorage.setItem("sizeup:signed-in", String(Date.now()));   // a new value each time: the event fires
    localStorage.removeItem("sizeup:signed-in");
  } catch (_) { /* no storage: the console checks again when it is looked at (focus) */ }
  try {
    if (window.opener && !window.opener.closed) window.opener.postMessage(MSG, location.origin);
  } catch (_) { /* the opener went elsewhere */ }
  const close = typeof document !== "undefined" && document.getElementById ? document.getElementById("reauth-close") : null;
  if (close) close.addEventListener("click", () => window.close());
  setTimeout(() => window.close(), 150);
})();
