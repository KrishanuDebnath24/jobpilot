/**
 * Status, plus an escape hatch.
 *
 * The content script only auto-runs on the ATS hosts JobPilot already knows how to
 * detect, which is the right default: an extension that reads every form on every site
 * is a much bigger thing to trust than one that reads the twenty platforms your jobs
 * actually live on. "Fill this page" covers the rest — the user's click is what grants
 * access to that tab, so an unknown ATS still works without widening the standing
 * permission for every site they visit.
 */

const stateEl = document.getElementById("state");
const stateText = document.getElementById("stateText");
const hint = document.getElementById("hint");
const apiEl = document.getElementById("api");

chrome.runtime.sendMessage({ kind: "status" }).then((res) => {
  const linked = !!res?.linked;
  stateEl.className = `state ${linked ? "ok" : "off"}`;
  stateText.textContent = linked ? "Linked to your JobPilot account" : "Not linked yet";
  hint.textContent = linked
    ? "Application forms on a known ATS fill themselves. Nothing is ever submitted for you."
    : "Open the JobPilot dashboard and sign in — this extension picks up that session automatically.";
  apiEl.textContent = res?.apiUrl ?? "";
});

document.getElementById("fill").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  try {
    // Already-injected frames answer this and return; the catch covers pages where no
    // content script is running yet.
    await chrome.tabs.sendMessage(tab.id, { kind: "fill-now" });
  } catch (_) {
    await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ["content/autofill.css"] });
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/autofill.js"] });
  }
  window.close();
});

document.getElementById("options").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
