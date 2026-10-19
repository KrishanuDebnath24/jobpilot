/**
 * Runs only on the JobPilot dashboard. Two jobs, both small.
 *
 * 1. Link the extension. The dashboard already holds a signed-in bearer token in
 *    localStorage; copying it into extension storage means there is no second login to
 *    maintain and no password anywhere in here. Same origin, same user, already
 *    authenticated — the extension is just borrowing the session it can already see.
 *
 * 2. Announce itself. The page cannot detect an extension on its own, so without this
 *    the Auto-apply button would have no way to tell "not installed" from "installed and
 *    quietly broken", and would have to show the same hopeful message either way.
 */

const TOKEN_KEY = "jobpilot_token";
const MARKER = "jobpilotAutoapply";

function announce() {
  // A data attribute rather than a global: the page reads it with a plain DOM query and
  // nothing has to be injected into the page's JS world.
  document.documentElement.dataset[MARKER] = chrome.runtime.getManifest().version;
}

function link() {
  let token = null;
  try {
    token = localStorage.getItem(TOKEN_KEY);
  } catch (_) {
    return; // storage blocked; nothing to link, and nothing worth reporting
  }
  if (!token) return;
  chrome.runtime.sendMessage({
    kind: "link",
    token,
    apiUrl: document.documentElement.dataset.jobpilotApi || undefined,
  });
}

announce();
link();

// Re-link on sign-in/sign-out. `storage` only fires for *other* tabs, so poll lightly as
// well — a token that changes in this tab is exactly the case that matters (the user just
// logged in), and a 5s check costs nothing next to getting the token wrong.
let last = null;
setInterval(() => {
  let token = null;
  try {
    token = localStorage.getItem(TOKEN_KEY);
  } catch (_) { /* ignore */ }
  if (token && token !== last) {
    last = token;
    link();
  }
}, 5000);

window.addEventListener("storage", (e) => {
  if (e.key === TOKEN_KEY && e.newValue) link();
});
