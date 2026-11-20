// /reauth (#818): the console's "Re-sign in" opens this page in a pop-up (else a new tab). It is behind Cloudflare
// Access like every page, so reaching it means the sign-in is fresh. It tells the console, to this origin only: the
// opener by postMessage (targetOrigin = this origin: never another site's window), and every console tab by
// BroadcastChannel (same origin by design; the opener may be gone when Access's login page cut the link or the tab
// was opened as a plain link). The message carries nothing: the console asks GET /api/session itself. Then it closes
// (a pop-up the console opened); a tab that can't be closed says "Signed in".
"use strict";

(() => {
  const MSG = { type: "sizeup:signed-in" };
  try {
    if (window.opener && !window.opener.closed) window.opener.postMessage(MSG, location.origin);
  } catch (_) { /* the opener went elsewhere: the channel below */ }
  try {
    const ch = new BroadcastChannel("sizeup-session");
    ch.postMessage(MSG);
    ch.close();
  } catch (_) { /* no BroadcastChannel: the console checks again when it is looked at (focus) */ }
  setTimeout(() => window.close(), 150);
})();
