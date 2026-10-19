const apiUrl = document.getElementById("apiUrl");
const token = document.getElementById("token");
const saved = document.getElementById("saved");

const DEFAULTS = { apiUrl: "http://localhost:8000", token: null };

chrome.storage.local.get(DEFAULTS).then((cfg) => {
  apiUrl.value = cfg.apiUrl ?? DEFAULTS.apiUrl;
  // The stored token is never rendered back — a settings page that shows a bearer token
  // in plain text is one shoulder-surf from an account. A placeholder says it's there.
  token.placeholder = cfg.token ? "linked — leave blank to keep" : "not linked yet";
});

document.getElementById("save").addEventListener("click", async () => {
  const next = { apiUrl: apiUrl.value.trim().replace(/\/+$/, "") || DEFAULTS.apiUrl };
  if (token.value.trim()) next.token = token.value.trim();
  await chrome.storage.local.set(next);
  token.value = "";
  saved.textContent = "Saved";
  setTimeout(() => { saved.textContent = ""; }, 2000);
});

document.getElementById("unlink").addEventListener("click", async () => {
  await chrome.storage.local.set({ token: null });
  token.placeholder = "not linked yet";
  saved.textContent = "Unlinked";
  setTimeout(() => { saved.textContent = ""; }, 2000);
});
