# JobPilot Auto-apply — browser extension

Fills a job application form from your JobPilot application profile and answer bank, in
your own browser, with the form in front of you. **It never presses Submit.**

## Why an extension

The dashboard is a web page, and browser security stops one origin's JavaScript from
reaching into another's. Nothing running on `localhost:3000` can type into a form on
`boards.greenhouse.io` — so the part that touches the form has to live in the browser
itself.

That constraint turns out to be the good design anyway. Filling in your own browser means
the form sees your real session (Workday and Greenhouse accounts keep working), the
employer sees an ordinary human filling in an ordinary form, there is no CAPTCHA to fight,
and you make the final click.

## Install

1. Start the JobPilot stack (`docker compose up`) and sign in at `http://localhost:3000`.
2. Open `chrome://extensions` (or `edge://extensions`).
3. Turn on **Developer mode**.
4. Click **Load unpacked** and choose this `extension/` folder.
5. Reload the JobPilot dashboard tab once. The extension picks up your signed-in session
   automatically — there is no second login.

Click the toolbar icon to confirm it says *Linked to your JobPilot account*.

## Using it

Every row in the dashboard's jobs table has an **Auto-apply** button. It opens that job's
resolved application form in a new tab, and the extension takes over from there:

- **Green fields** were filled from something you authored — your profile, or an answer
  you have already given. Each one is labelled with which.
- **Amber fields** are gaps. Nothing has an answer for them yet, so they are left empty
  and listed in the panel. Click one to jump to it.
- **Red fields** are hard stops: a CAPTCHA, or a work-eligibility question that names no
  single country and therefore cannot be answered from a stored value.
- Your tailored CV for that job is attached automatically when one has been exported;
  otherwise your uploaded original is.

When you press the form's own Submit button, whatever you typed into the gaps is banked —
so the next form asks you less. Answers that came out of the bank unchanged are counted as
reuse rather than rewritten.

## What it can and can't reach

The extension runs automatically on the ~20 ATS platforms JobPilot already knows how to
detect (Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Workable, Teamtailor,
Personio, Recruitee, BambooHR, Jobvite, Breezy, Pinpoint, iCIMS, Taleo, SuccessFactors,
Oracle Cloud, Rezoomo, Occupop). That list is the extension's entire standing permission —
it cannot read any other site.

For a form somewhere else, open it and click **Fill this page** in the toolbar popup. Your
click is what grants access to that one tab, which is a much smaller thing to hand over
than permission to read every site you visit.

## What it sends where

| | |
|---|---|
| **To JobPilot** (`localhost:8000`) | The form's field labels, types and options — and, on submit, the answers you gave. |
| **To the employer** | Only what you would have typed yourself. The job id travels in a URL fragment, which browsers never send to a server. |
| **Anywhere else** | Nothing. |

Your bearer token lives in the extension's service worker, never in a content script, so
no employer's page can read it.

## Files

| File | What it does |
|---|---|
| `background.js` | The only part that talks to JobPilot. Holds the token, makes every API call. |
| `content/dashboard.js` | Runs on the dashboard. Links the session, announces the extension to the page. |
| `content/autofill.js` | Runs on ATS pages. Reads the form, fills it, records what you submitted. |
| `content/autofill.css` | The provenance highlighting and the summary panel. |
| `popup.html` / `popup.js` | Status, and "Fill this page" for unlisted sites. |
| `options.html` / `options.js` | API URL, manual token, unlink. |

## Troubleshooting

**"Not linked yet"** — open the dashboard and sign in, then reload that tab.

**Nothing fills, no panel** — the page may put its form in a cross-origin iframe from a
host outside the match list. Try **Fill this page**.

**Fields fill and then go blank** — an unusual framework is reverting them. Worth a bug
report with the ATS name; the native-setter path in `autofill.js` handles React, Vue and
plain forms, but not everything.

**Everything is amber on the first form** — expected. The bank starts empty. It gets
quieter with every application you send.
