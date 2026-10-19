/**
 * The only part of the extension that talks to JobPilot.
 *
 * Content scripts run on employers' pages, so they are the last place a bearer token
 * should live: anything on the page can read a variable a content script holds in a
 * shared world, and a token that leaks is a token that reads the whole account. So the
 * token stays here, in the service worker, and content scripts ask for *answers* rather
 * than for credentials.
 *
 * The same split is why fetches happen here. A service-worker fetch backed by
 * `host_permissions` is exempt from CORS, so the API keeps its origin allowlist tight
 * (just the dashboard) instead of having to admit every ATS domain a form might live on.
 */

const DEFAULTS = { apiUrl: "http://localhost:8000", token: null };

async function config() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

/** Call the JobPilot API. Errors are returned, never thrown: a content script that
 *  cannot reach the API must degrade to leaving the form alone, not to a broken page. */
async function api(path, { method = "GET", body } = {}) {
  const { apiUrl, token } = await config();
  if (!token) {
    return { ok: false, status: 401, error: "Not signed in — open the JobPilot dashboard once to link this extension." };
  }
  let res;
  try {
    res = await fetch(`${apiUrl}${path}`, {
      method,
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  } catch (e) {
    return { ok: false, status: 0, error: `Can't reach JobPilot at ${apiUrl} — is the stack running?` };
  }
  if (res.status === 401) {
    // The stored token has expired. Clearing it makes the next dashboard visit re-link,
    // rather than leaving every form silently unfilled with no explanation.
    await chrome.storage.local.set({ token: null });
    return { ok: false, status: 401, error: "Your JobPilot session expired — open the dashboard to sign in again." };
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch (_) { /* body wasn't json */ }
    return { ok: false, status: res.status, error: detail };
  }
  return { ok: true, status: res.status, data: await res.json() };
}

/** Fetch a CV as a data URL, so the content script can rebuild it as a File.
 *  Transferred as base64 because structured cloning a Blob across the extension message
 *  boundary is not reliable across browsers, whereas a string always survives. */
async function fetchCv(path) {
  const { apiUrl, token } = await config();
  if (!token) return { ok: false, error: "not signed in" };
  try {
    const res = await fetch(`${apiUrl}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!res.ok) return { ok: false, error: `CV download failed (HTTP ${res.status})` };
    const buf = await res.arrayBuffer();
    let binary = "";
    const bytes = new Uint8Array(buf);
    const CHUNK = 0x8000; // btoa on the whole array blows the argument limit on big PDFs
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return {
      ok: true,
      base64: btoa(binary),
      type: res.headers.get("content-type") || "application/pdf",
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/**
 * Which job a tab is applying to.
 *
 * The dashboard opens the apply URL with `#jobpilot=<id>`, but only the frame holding
 * that URL can read it — and plenty of ATS pages put the actual form in a cross-origin
 * iframe that sees a different location entirely. The service worker is the one place
 * every frame in a tab can reach, so it holds the association and hands it back to
 * whichever frame asks. Session-scoped: a job is a property of this browsing session,
 * and stale ids surviving a browser restart would misattribute answers.
 */
async function rememberJob(tabId, jobId) {
  if (tabId == null || !jobId) return;
  await chrome.storage.session.set({ [`job:${tabId}`]: jobId });
}

async function recallJob(tabId) {
  if (tabId == null) return null;
  const key = `job:${tabId}`;
  return (await chrome.storage.session.get(key))[key] ?? null;
}

chrome.tabs?.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(`job:${tabId}`);
});

const HANDLERS = {
  /** The dashboard content script hands over the signed-in user's token. */
  async link({ token, apiUrl }) {
    const current = await config();
    const next = {};
    if (token && token !== current.token) next.token = token;
    if (apiUrl && apiUrl !== current.apiUrl) next.apiUrl = apiUrl;
    if (Object.keys(next).length) await chrome.storage.local.set(next);
    return { ok: true, linked: !!(next.token ?? current.token) };
  },

  async status() {
    const { apiUrl, token } = await config();
    return { ok: true, apiUrl, linked: !!token };
  },

  async plan({ fields, jobId, url }, sender) {
    const tabId = sender?.tab?.id;
    if (jobId) await rememberJob(tabId, jobId);
    const job = jobId ?? (await recallJob(tabId));
    return api("/apply/plan", {
      method: "POST",
      body: { fields, job_id: job ?? null, url: url ?? null },
    });
  },

  async record({ answers, jobId, cv }, sender) {
    const job = jobId ?? (await recallJob(sender?.tab?.id));
    return api("/apply/record", {
      method: "POST",
      body: { answers, job_id: job ?? null, cv: cv ?? null },
    });
  },

  async cv({ path }) {
    return fetchCv(path);
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = HANDLERS[msg?.kind];
  if (!handler) return false;
  // Returning true keeps the message channel open for the async reply below.
  handler(msg, sender).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
  return true;
});
