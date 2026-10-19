/**
 * The part that touches a real application form.
 *
 * Everything here happens in the user's own browser, in their own session, with the page
 * in front of them: it reads the form, asks JobPilot what belongs in each field, types
 * the answers, and marks each one with where it came from. It does not press Submit, and
 * there is no code path in this file that could — the only click it ever listens for is
 * the user's own, and that listener records what was sent rather than sending it.
 *
 * Three things are less obvious than they look:
 *
 * - **Only empty fields are filled.** A field that already has a value was either
 *   pre-filled by the ATS from an account or typed by the user, and both beat a stored
 *   answer. Overwriting is how an autofill quietly changes an answer someone meant.
 * - **Values are set through the native setter.** React tracks its own copy of an input's
 *   value; assigning `el.value` leaves that copy stale, so the framework reverts the
 *   field on the next render and the form submits empty. Going through the prototype's
 *   setter and clearing `_valueTracker` is what makes the typed value survive.
 * - **Re-scanning is required, not defensive.** Workday and Ashby build their forms in
 *   stages, so the fields that exist at load are rarely all the fields that exist at
 *   submit. A mutation observer picks up each new step and plans only what is new.
 */

(() => {
  if (window.__jobpilotAutofill) return; // one instance per frame, survives re-injection
  window.__jobpilotAutofill = true;

  const HASH_KEY = "jobpilot";
  const REF_ATTR = "data-jp-ref";
  const MIN_FIELDS = 2; // a lone search box is not an application form

  /** Fields we have planned, by ref. Holds what we filled so `edited` is knowable later. */
  const tracked = new Map();
  let jobId = null;
  let recorded = false;
  /**
   * What we put in the CV field, once we have actually put it there.
   *
   * Kept at frame scope rather than per-plan because a multi-step form asks for the CV on
   * step one and never again: by the step that carries the Submit button, `tracked` no
   * longer holds a CV field at all. Only a confirmed attach writes here, so the absence of
   * a value means "no file went on from us", never "we did not look".
   */
  let attachedCv = null;
  let refSeq = 0;
  let panel = null;

  // ------------------------------------------------------------------ //
  // Job identity
  // ------------------------------------------------------------------ //
  function readJobId() {
    const match = /[#&]jobpilot=([\w-]+)/.exec(location.hash);
    if (!match) return null;
    const id = match[1];
    // Strip it straight away: the fragment is ours, not the employer's, and leaving it
    // in the address bar invites it into a bookmark or a shared link.
    const clean = location.hash.replace(/[#&]jobpilot=[\w-]+/, "").replace(/^#&/, "#");
    history.replaceState(null, "", location.pathname + location.search + (clean === "#" ? "" : clean));
    return id;
  }

  // ------------------------------------------------------------------ //
  // Reading the form
  // ------------------------------------------------------------------ //
  const IGNORED_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);

  function visible(el) {
    if (el.disabled || el.readOnly) return false;
    if (el.type === "hidden") return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none";
  }

  function cleanText(node) {
    return (node?.textContent || "").replace(/\s+/g, " ").trim();
  }

  /**
   * The question a field is asking, in the words the form uses.
   *
   * Tried in descending order of how deliberate the signal is: an explicit label element
   * is authored intent, a nearby heading is a guess. The guesses matter anyway — Workday
   * labels almost nothing with `for`, and without the fallbacks half its fields would
   * arrive at the resolver with no question at all and escalate on every form forever.
   */
  function labelFor(el) {
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return cleanText(label);
    }
    const wrapping = el.closest("label");
    if (wrapping) return cleanText(wrapping);

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => cleanText(document.getElementById(id)))
        .filter(Boolean)
        .join(" ");
      if (text) return text;
    }
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();

    // A fieldset's legend is the question for every control inside it — which is exactly
    // how radio groups ask things.
    const legend = el.closest("fieldset")?.querySelector("legend");
    if (legend) return cleanText(legend);

    // Last resort: the nearest preceding text in the field's own container. Capped in
    // length because grabbing a whole paragraph produces a question nothing can match.
    let node = el.parentElement;
    for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
      const candidate = Array.from(node.childNodes)
        .filter((n) => n.nodeType === Node.TEXT_NODE || (n.nodeType === Node.ELEMENT_NODE && !n.contains(el)))
        .map(cleanText)
        .filter((t) => t.length > 1 && t.length < 160)
        .pop();
      if (candidate) return candidate;
    }
    return "";
  }

  function isRequired(el) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    // Most ATS mark required fields with an asterisk in the label rather than the
    // attribute, and "required" is what decides whether a gap blocks the user.
    return /\*\s*$/.test(labelFor(el));
  }

  function optionsOf(el) {
    if (el.tagName === "SELECT") {
      return Array.from(el.options)
        .filter((o) => o.value !== "" && !/^(please\s+)?(select|choose)\b/i.test(o.text.trim()))
        .map((o) => ({ value: o.value, label: o.text.trim() }));
    }
    return null;
  }

  /** Radio groups collapse to one logical field: one question, several offered answers. */
  function radioGroups(root) {
    const groups = new Map();
    root.querySelectorAll('input[type="radio"]').forEach((el) => {
      if (!visible(el)) return;
      const name = el.name || labelFor(el.closest("fieldset") || el) || "";
      if (!name) return;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(el);
    });
    return groups;
  }

  /** Has this element already been planned? Re-scans must only report what is new. */
  function seen(el) {
    const ref = el.getAttribute(REF_ATTR);
    return !!ref && tracked.has(ref);
  }

  function descriptorFor(el, { type, options }) {
    const ref = el.getAttribute(REF_ATTR) || `jp-${(refSeq += 1)}`;
    el.setAttribute(REF_ATTR, ref);
    return {
      ref,
      label: labelFor(el),
      placeholder: el.placeholder || null,
      name: el.name || null,
      type,
      autocomplete: el.getAttribute("autocomplete") || null,
      required: isRequired(el),
      options: options ?? null,
    };
  }

  /** Every answerable field on the page, as plain descriptors the server can reason about. */
  function scrape() {
    const fields = [];
    const elements = new Map(); // ref -> element(s), kept out of the payload

    const groups = radioGroups(document);
    groups.forEach((members, name) => {
      const first = members[0];
      // A group whose members already carry a planned ref was handled on an earlier pass.
      // Without this the observer would re-plan every radio on the page on every DOM
      // change, stacking a fresh badge beside each one.
      if (members.some(seen)) return;
      const legend = first.closest("fieldset")?.querySelector("legend");
      const question = cleanText(legend) || labelFor(first) || name;
      const options = members.map((m) => ({
        value: m.value,
        label: labelFor(m) || m.value,
      }));
      const ref = `jp-${(refSeq += 1)}`;
      members.forEach((m) => m.setAttribute(REF_ATTR, ref));
      fields.push({
        ref,
        label: question,
        placeholder: null,
        name,
        type: "radio",
        autocomplete: null,
        required: members.some(isRequired),
        options,
      });
      elements.set(ref, members);
    });

    document.querySelectorAll("input, select, textarea").forEach((el) => {
      const tag = el.tagName;
      const type = tag === "TEXTAREA" ? "textarea" : tag === "SELECT" ? "select" : (el.type || "text").toLowerCase();
      if (type === "radio") return; // already handled as a group
      if (IGNORED_TYPES.has(type)) return;
      if (!visible(el)) return;
      if (seen(el)) return;

      const descriptor = descriptorFor(el, { type, options: optionsOf(el) });
      fields.push(descriptor);
      elements.set(descriptor.ref, [el]);
    });

    return { fields, elements };
  }

  // ------------------------------------------------------------------ //
  // Writing to the form
  // ------------------------------------------------------------------ //
  /**
   * Set a value the way a framework will believe.
   *
   * React attaches a `_valueTracker` to inputs and compares against it to decide whether
   * a change really happened; a plain `el.value = x` updates the DOM but not the tracker,
   * so React sees no change, re-renders from its own state, and wipes the field. Setting
   * through the prototype's setter and clearing the tracker makes the synthetic input
   * event indistinguishable from typing.
   */
  function setNativeValue(el, value) {
    const proto =
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (el._valueTracker) el._valueTracker.setValue(" ");
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  function fire(el, ...types) {
    types.forEach((type) => el.dispatchEvent(new Event(type, { bubbles: true })));
  }

  function currentValue(entry) {
    const [first] = entry.elements;
    if (!first) return "";
    if (entry.plan.type === "radio") {
      const chosen = entry.elements.find((el) => el.checked);
      return chosen ? (labelFor(chosen) || chosen.value) : "";
    }
    if (first.type === "checkbox") return first.checked ? "Yes" : "";
    if (first.tagName === "SELECT") {
      return first.selectedOptions[0]?.text?.trim() ?? "";
    }
    return (first.value || "").trim();
  }

  function alreadyAnswered(entry) {
    return currentValue(entry) !== "";
  }

  function writeValue(entry) {
    const { plan, elements } = entry;
    const [el] = elements;
    if (!el) return false;

    if (plan.type === "radio") {
      const wanted = plan.option_value ?? plan.value;
      const target = elements.find(
        (m) => m.value === wanted || (labelFor(m) || "").trim() === plan.value,
      );
      if (!target) return false;
      target.checked = true;
      fire(target, "input", "change");
      target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      return true;
    }

    if (el.type === "checkbox") {
      const yes = /^(yes|true|y)$/i.test(String(plan.value));
      if (el.checked !== yes) {
        el.checked = yes;
        fire(el, "input", "change");
      }
      return true;
    }

    if (el.tagName === "SELECT") {
      const wanted = plan.option_value ?? plan.value;
      const option = Array.from(el.options).find(
        (o) => o.value === wanted || o.text.trim() === plan.value,
      );
      if (!option) return false;
      setNativeValue(el, option.value);
      fire(el, "input", "change");
      return true;
    }

    setNativeValue(el, String(plan.value ?? ""));
    fire(el, "input", "change");
    fire(el, "blur");
    return true;
  }

  /** Rebuild the CV as a real File and drop it on the input, as a manual pick would. */
  async function attachCv(entry, cv) {
    const [el] = entry.elements;
    if (!el || !cv?.path) return false;
    if (el.files && el.files.length > 0) return false; // the user already attached one
    const res = await chrome.runtime.sendMessage({ kind: "cv", path: cv.path });
    if (!res?.ok) return false;
    try {
      const bytes = Uint8Array.from(atob(res.base64), (c) => c.charCodeAt(0));
      const file = new File([bytes], cv.filename || "CV.pdf", { type: res.type });
      const dt = new DataTransfer();
      dt.items.add(file);
      el.files = dt.files;
      fire(el, "input", "change");
      return true;
    } catch (e) {
      return false;
    }
  }

  // ------------------------------------------------------------------ //
  // Showing the work
  // ------------------------------------------------------------------ //
  const TIER_LABEL = {
    profile: "your profile",
    bank_exact: "answered before",
    bank_near: "answered before (similar wording)",
  };

  function markField(entry, state) {
    const [el] = entry.elements;
    if (!el) return;
    const anchor = el.type === "radio" || el.type === "checkbox" ? el.closest("fieldset") || el.parentElement : el;
    if (!anchor) return;
    anchor.classList.remove("jp-filled", "jp-ask", "jp-blocked");
    anchor.classList.add(state === "kept" ? "jp-filled" : `jp-${state}`);

    const existing = anchor.parentElement?.querySelector(`:scope > .jp-badge[data-ref="${entry.plan.ref}"]`);
    existing?.remove();

    const badge = document.createElement("span");
    badge.className = `jp-badge jp-badge-${state}`;
    badge.dataset.ref = entry.plan.ref;
    if (state === "kept") {
      // Deliberately not "from your profile": we did not put this here, and a badge that
      // claims provenance we don't have is the one kind of dishonesty this UI can commit.
      badge.className = "jp-badge jp-badge-filled";
      badge.textContent = "already filled in — left as it was";
    } else if (state === "filled") {
      const tier = TIER_LABEL[entry.plan.tier] || "JobPilot";
      badge.textContent = `from ${tier}`;
      if (entry.plan.similarity && entry.plan.tier === "bank_near") {
        badge.title = `Matched a question you answered before (${Math.round(entry.plan.similarity * 100)}% similar). Check it reads right.`;
      }
    } else {
      badge.textContent = entry.plan.reason || "needs you";
      badge.title = entry.plan.reason || "";
    }
    anchor.insertAdjacentElement("afterend", badge);
  }

  function renderPanel(summary, gaps, note) {
    if (!panel) {
      panel = document.createElement("div");
      panel.className = "jp-panel";
      document.body.appendChild(panel);
    }
    const line = (n, text) => (n ? `<li><b>${n}</b> ${text}</li>` : "");
    panel.innerHTML = `
      <div class="jp-panel-head">
        <span class="jp-dot"></span>
        <strong>JobPilot</strong>
        <button class="jp-close" title="Hide">×</button>
      </div>
      <p class="jp-lede">${summary.filled} of ${summary.total} fields filled from what you've already told us.</p>
      <ul class="jp-stats">
        ${line(summary.from_profile, "from your profile")}
        ${line(summary.from_bank, "reused from your answer bank")}
        ${line(summary.attachments, "CV attached")}
        ${line(summary.asked, "left for you")}
        ${line(summary.blocked, "blocked on purpose")}
      </ul>
      ${gaps.length ? `<p class="jp-gaps-title">Needs you</p><ol class="jp-gaps">${gaps
        .map((g) => `<li data-ref="${g.ref}"><span>${g.label || "(unlabelled field)"}</span><em>${g.reason || ""}</em></li>`)
        .join("")}</ol>` : ""}
      ${note ? `<p class="jp-note">${note}</p>` : ""}
      <p class="jp-foot">Nothing is submitted. Press the form's own Submit when you're happy — your answers get banked then.</p>
    `;
    panel.querySelector(".jp-close").onclick = () => panel.remove();
    panel.querySelectorAll(".jp-gaps li").forEach((li) => {
      li.onclick = () => {
        const entry = tracked.get(li.dataset.ref);
        const el = entry?.elements?.[0];
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.focus({ preventScroll: true });
      };
    });
  }

  function renderError(message) {
    if (!panel) {
      panel = document.createElement("div");
      panel.className = "jp-panel";
      document.body.appendChild(panel);
    }
    panel.innerHTML = `
      <div class="jp-panel-head"><span class="jp-dot jp-dot-warn"></span><strong>JobPilot</strong>
      <button class="jp-close" title="Hide">×</button></div>
      <p class="jp-lede">${message}</p>`;
    panel.querySelector(".jp-close").onclick = () => panel.remove();
  }

  // ------------------------------------------------------------------ //
  // The run
  // ------------------------------------------------------------------ //
  let running = false;

  async function run({ announce = true } = {}) {
    if (running) return;
    running = true;
    try {
      const { fields, elements } = scrape();
      if (!fields.length) return;
      if (tracked.size === 0 && fields.length < MIN_FIELDS) return;

      const res = await chrome.runtime.sendMessage({
        kind: "plan",
        fields,
        jobId,
        url: location.href,
      });
      if (!res?.ok) {
        if (announce && res?.error) renderError(res.error);
        return;
      }

      const plan = res.data;
      for (const item of plan.fields) {
        const els = elements.get(item.ref);
        if (!els) continue;
        const entry = { plan: item, elements: els, written: null };
        tracked.set(item.ref, entry);

        if (item.action === "fill") {
          if (alreadyAnswered(entry)) {
            // Already carries an answer — the ATS pre-filled it from an account, or the
            // user typed it. Both beat a stored answer, so leave it, and say so rather
            // than taking credit for a value we did not put there.
            entry.written = null;
            markField(entry, "kept");
            continue;
          }
          if (writeValue(entry)) entry.written = currentValue(entry);
        } else if (item.action === "attach_cv") {
          if (await attachCv(entry, plan.cv)) {
            entry.written = "(CV attached)";
            // Which file went on, for the application log. `tailored` comes from the
            // server's own resolution, so this reports its decision back rather than
            // guessing at one.
            attachedCv = {
              attached: true,
              source: plan.cv?.tailored ? "tailored" : "original",
              draft_id: plan.cv?.draft_id ?? null,
            };
          } else markField(entry, "ask");
        } else if (item.action === "ask" || item.action === "blocked") {
          markField(entry, item.action === "blocked" ? "blocked" : "ask");
          continue;
        } else {
          continue;
        }
        if (entry.written !== null) markField(entry, "filled");
      }

      if (announce) {
        const gaps = plan.fields.filter((f) => f.action === "ask" || f.action === "blocked");
        const note = plan.profile_complete
          ? ""
          : "Your application profile is missing a name or email — filling those in on the JobPilot dashboard will cover them on every future form.";
        renderPanel(plan.summary, gaps, note);
      }
    } finally {
      running = false;
    }
  }

  // ------------------------------------------------------------------ //
  // Learning from what was submitted
  // ------------------------------------------------------------------ //
  /**
   * Read every tracked field back as it stands, and send it to be banked.
   *
   * `edited` is computed here rather than server-side because only this frame knows what
   * was in the box when we let go of it: a value that still matches what we typed is a
   * reuse, and one that does not is the user correcting us — which is precisely the
   * signal worth storing.
   */
  function harvest() {
    const answers = [];
    tracked.forEach((entry) => {
      const { plan } = entry;
      if (plan.action === "attach_cv" || plan.action === "skip") return;
      if (plan.bankable === false) return; // a captcha's answer is noise, not knowledge
      const value = currentValue(entry);
      if (!value) return;
      answers.push({
        question: plan.label,
        value,
        kind: plan.type === "select" || plan.type === "radio" ? "select"
          : plan.type === "checkbox" ? "boolean"
          : plan.type === "number" ? "number"
          : plan.type === "date" ? "date"
          : "text",
        tier: plan.tier,
        source_id: plan.source_id,
        edited: entry.written !== null && entry.written !== value,
      });
    });
    return answers;
  }

  /**
   * The user pressed Submit: bank the answers, and log the application.
   *
   * The empty-answers guard is what keeps the log honest. The click listener below fires
   * on any control reading as "Apply", including the button on a listing page that merely
   * navigates to the form — but that page has no filled fields behind it, so it harvests
   * nothing and records nothing. An application only ever gets logged off a form this
   * script had already planned and filled.
   */
  function record() {
    if (recorded) return;
    const answers = harvest();
    if (!answers.length) return;
    recorded = true;
    // Fire-and-forget: the user's submit must never wait on us, and a failed bank costs
    // a repeated question next time, nothing more. Wrapped because an extension reload
    // invalidates the context and `sendMessage` then throws synchronously — which, from
    // inside a capture-phase submit listener, would break the user's submit.
    try {
      chrome.runtime.sendMessage({ kind: "record", answers, jobId, cv: attachedCv })
        ?.catch?.(() => {});
    } catch (_) { /* extension context gone; the form still submits */ }
    // Re-arm after a moment so a multi-step form banks each step it completes.
    setTimeout(() => { recorded = false; }, 4000);
  }

  document.addEventListener("submit", record, true);

  // Most ATS forms are JS-driven and never fire a real `submit`, so the user's click on
  // anything that reads as a submit control is the more reliable signal. Listening in the
  // capture phase means we read the values before the page starts tearing the form down.
  document.addEventListener(
    "click",
    (e) => {
      const el = e.target instanceof Element ? e.target.closest("button, input[type=submit], a") : null;
      if (!el) return;
      const text = (el.innerText || el.value || "").trim();
      if (/^(submit|submit application|send application|apply now|apply)$/i.test(text)) record();
    },
    true,
  );

  // ------------------------------------------------------------------ //
  // Start, and keep up with multi-step forms
  // ------------------------------------------------------------------ //
  jobId = readJobId();

  let pending = null;
  const observer = new MutationObserver(() => {
    clearTimeout(pending);
    // Debounced: an ATS that re-renders on every keystroke would otherwise re-plan on
    // every keystroke, and the plan endpoint is not free.
    pending = setTimeout(() => run({ announce: false }), 900);
  });

  function start() {
    run({ announce: true });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }

  // The toolbar popup can ask for a fill on a page outside the known-ATS list.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.kind !== "fill-now") return false;
    tracked.clear();
    run({ announce: true }).then(() => sendResponse({ ok: true }));
    return true;
  });
})();
