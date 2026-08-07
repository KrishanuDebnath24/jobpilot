"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { AnswerBankEntry, AnswerKind, ApplicationProfile } from "@/lib/types";
import {
  deleteAnswer,
  getAnswerBank,
  getApplicationProfile,
  seedApplicationProfile,
  updateApplicationProfile,
  upsertAnswer,
} from "@/lib/api";

/* Stage 1 of the apply agent: the facts an agent may submit on your behalf.
 *
 * Two surfaces, matching the two stores. The profile holds stable facts and is seeded
 * from your parsed résumé so you confirm rather than retype. The answer bank holds
 * screening questions you've already answered, which is what lets later applications
 * fill themselves without anything being invented.
 *
 * Nothing here is model-generated, and the UI says so where it matters — the locked
 * badge is not decoration, it marks the questions that will always come back to you
 * rather than being matched by similarity. */

const JURISDICTIONS: { code: string; label: string }[] = [
  { code: "IE", label: "Ireland" },
  { code: "UK", label: "United Kingdom" },
  { code: "EU", label: "European Union" },
  { code: "US", label: "United States" },
];

const WORK_MODES = ["", "remote", "hybrid", "onsite"];

const input =
  "w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100";
const labelCls = "block space-y-1";
const labelText = "text-xs font-medium text-slate-600";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={labelCls}>
      <span className={labelText}>{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-slate-400">{hint}</span>}
    </label>
  );
}

function SectionCard({
  title,
  blurb,
  locked = false,
  children,
}: {
  title: string;
  blurb: string;
  locked?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      className={`overflow-hidden rounded-2xl border bg-white shadow-card ${
        locked ? "border-amber-200" : "border-slate-200"
      }`}
    >
      <div
        className={`border-b px-4 py-3 ${
          locked ? "border-amber-100 bg-amber-50/50" : "border-slate-100"
        }`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
          {locked && (
            <span
              className="rounded-full border border-amber-200 bg-white px-2 py-0.5 text-[10px] font-medium text-amber-700"
              title="Answered only from what you set here — never matched by similarity"
            >
              locked
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{blurb}</p>
      </div>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

export default function ApplicationProfileEditor() {
  const [profile, setProfile] = useState<ApplicationProfile | null>(null);
  const [entries, setEntries] = useState<AnswerBankEntry[]>([]);
  const [dirty, setDirty] = useState<Partial<ApplicationProfile>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // New-answer composer
  const [q, setQ] = useState("");
  const [a, setA] = useState("");
  const [kind, setKind] = useState<AnswerKind>("text");
  const [scope, setScope] = useState("global");

  const load = useCallback(async () => {
    try {
      const [p, e] = await Promise.all([getApplicationProfile(), getAnswerBank()]);
      setProfile(p);
      setEntries(e);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // The edited view: saved profile with any unsaved edits laid over it.
  const view = useMemo(
    () => ({ ...(profile ?? ({} as ApplicationProfile)), ...dirty }),
    [profile, dirty],
  );
  const hasChanges = Object.keys(dirty).length > 0;

  const set = <K extends keyof ApplicationProfile>(key: K, value: ApplicationProfile[K]) =>
    setDirty((d) => ({ ...d, [key]: value }));

  const setEligibility = (code: string, key: string, value: unknown) =>
    setDirty((d) => {
      const base = (d.eligibility_json ?? view.eligibility_json ?? {}) as Record<
        string,
        Record<string, unknown>
      >;
      return {
        ...d,
        eligibility_json: { ...base, [code]: { ...(base[code] ?? {}), [key]: value } },
      };
    });

  const save = async () => {
    if (!hasChanges) return;
    setBusy("save");
    setError(null);
    setNote(null);
    try {
      setProfile(await updateApplicationProfile(dirty));
      setDirty({});
      setNote("Profile saved.");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  };

  const seed = async () => {
    setBusy("seed");
    setError(null);
    setNote(null);
    try {
      const res = await seedApplicationProfile();
      if (res.seeded && res.profile) {
        setProfile(res.profile);
        setDirty({});
        setNote(`Filled ${res.fields?.length ?? 0} blank field(s) from your résumé.`);
      } else {
        setNote(res.reason ?? "Nothing to seed.");
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  };

  const addAnswer = async () => {
    const question = q.trim();
    if (!question || !a.trim()) return;
    setBusy("answer");
    setError(null);
    try {
      const res = await upsertAnswer({
        question_text: question,
        answer_value: a.trim(),
        answer_kind: kind,
        scope,
      });
      setQ("");
      setA("");
      setNote(
        res.locked_as
          ? `Saved as a locked ${res.locked_as.replace(/_/g, " ")} answer — it will only ever be reused on an exact match.`
          : "Answer saved.",
      );
      await load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  };

  const removeAnswer = async (id: string) => {
    setBusy(`del-${id}`);
    try {
      await deleteAnswer(id);
      setEntries((rows) => rows.filter((r) => r.id !== id));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(null);
    }
  };

  const lockedCount = entries.filter((e) => e.locked).length;

  return (
    <div className="space-y-4">
      {/* --- action bar --- */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white p-3 shadow-card">
        <p className="text-xs leading-relaxed text-slate-500">
          These are the only values an apply agent may submit for you. Anything not here
          gets asked, never guessed.
        </p>
        <div className="ml-auto flex flex-wrap gap-2">
          <button
            onClick={seed}
            disabled={!!busy}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98] disabled:opacity-40"
            title="Fill blank fields from your parsed résumé — never overwrites what you've typed"
          >
            {busy === "seed" ? "Seeding…" : "Fill blanks from résumé"}
          </button>
          <button
            onClick={save}
            disabled={!!busy || !hasChanges}
            className="rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-indigo-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-indigo-600"
          >
            {busy === "save" ? "Saving…" : hasChanges ? `Save ${Object.keys(dirty).length} change(s)` : "Saved"}
          </button>
        </div>
      </div>

      {note && (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {note}
        </p>
      )}
      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard title="Identity" blurb="Seeded from your résumé's contact details.">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="First name">
              <input className={input} value={view.first_name ?? ""} onChange={(e) => set("first_name", e.target.value)} />
            </Field>
            <Field label="Last name">
              <input className={input} value={view.last_name ?? ""} onChange={(e) => set("last_name", e.target.value)} />
            </Field>
            <Field label="Email">
              <input className={input} type="email" value={view.email ?? ""} onChange={(e) => set("email", e.target.value)} />
            </Field>
            <Field label="Phone">
              <input className={input} value={view.phone ?? ""} onChange={(e) => set("phone", e.target.value)} />
            </Field>
            <Field label="City">
              <input className={input} value={view.city ?? ""} onChange={(e) => set("city", e.target.value)} />
            </Field>
            <Field label="Country">
              <input className={input} value={view.country ?? ""} onChange={(e) => set("country", e.target.value)} />
            </Field>
            <Field label="Address">
              <input className={input} value={view.address_line1 ?? ""} onChange={(e) => set("address_line1", e.target.value)} />
            </Field>
            <Field label="Postcode / Eircode">
              <input className={input} value={view.postcode ?? ""} onChange={(e) => set("postcode", e.target.value)} />
            </Field>
          </div>
        </SectionCard>

        <SectionCard title="Links" blurb="Detected from your résumé where present.">
          <div className="grid gap-3">
            <Field label="LinkedIn">
              <input className={input} value={view.linkedin_url ?? ""} onChange={(e) => set("linkedin_url", e.target.value)} placeholder="https://linkedin.com/in/…" />
            </Field>
            <Field label="GitHub">
              <input className={input} value={view.github_url ?? ""} onChange={(e) => set("github_url", e.target.value)} placeholder="https://github.com/…" />
            </Field>
            <Field label="Portfolio / website">
              <input className={input} value={view.portfolio_url ?? ""} onChange={(e) => set("portfolio_url", e.target.value)} />
            </Field>
          </div>
        </SectionCard>

        <SectionCard title="Logistics" blurb="The questions almost every application asks.">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Notice period (days)">
              <input
                className={input}
                type="number"
                min={0}
                value={view.notice_period_days ?? ""}
                onChange={(e) => set("notice_period_days", e.target.value === "" ? null : Number(e.target.value))}
              />
            </Field>
            <Field label="Earliest start date">
              <input
                className={input}
                type="date"
                value={view.earliest_start_date ?? ""}
                onChange={(e) => set("earliest_start_date", e.target.value || null)}
              />
            </Field>
            <Field label="Salary expectation">
              <input
                className={input}
                type="number"
                min={0}
                value={view.salary_expectation ?? ""}
                onChange={(e) => set("salary_expectation", e.target.value === "" ? null : Number(e.target.value))}
              />
            </Field>
            <Field label="Currency">
              <input
                className={input}
                maxLength={3}
                placeholder="EUR"
                value={view.salary_currency ?? ""}
                onChange={(e) => set("salary_currency", e.target.value.toUpperCase())}
              />
            </Field>
            <Field label="Work mode preference">
              <select
                className={`${input} cursor-pointer capitalize`}
                value={view.work_mode_preference ?? ""}
                onChange={(e) => set("work_mode_preference", e.target.value || null)}
              >
                {WORK_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m || "—"}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Willing to relocate">
              <select
                className={`${input} cursor-pointer`}
                value={view.willing_to_relocate === null || view.willing_to_relocate === undefined ? "" : String(view.willing_to_relocate)}
                onChange={(e) =>
                  set("willing_to_relocate", e.target.value === "" ? null : e.target.value === "true")
                }
              >
                <option value="">—</option>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            </Field>
          </div>
        </SectionCard>

        <SectionCard
          title="Work eligibility"
          locked
          blurb="Stored per country on purpose. “Authorized to work in the US?” and “…in Ireland?” read almost identically to a machine but have different answers, so these are never matched by similarity — only by the exact country asked about."
        >
          <div className="space-y-3">
            {JURISDICTIONS.map(({ code, label }) => {
              const block = (view.eligibility_json?.[code] ?? {}) as Record<string, unknown>;
              const authorized = block.authorized;
              const sponsorship = block.sponsorship_required;
              return (
                <div key={code} className="grid grid-cols-3 items-center gap-2 border-b border-slate-100 pb-2 last:border-0 last:pb-0">
                  <span className="text-xs font-medium text-slate-700">
                    {label} <span className="font-mono text-[10px] text-slate-400">{code}</span>
                  </span>
                  <select
                    className={`${input} cursor-pointer py-1 text-xs`}
                    value={authorized === undefined || authorized === null ? "" : String(authorized)}
                    onChange={(e) =>
                      setEligibility(code, "authorized", e.target.value === "" ? null : e.target.value === "true")
                    }
                    aria-label={`Authorized to work in ${label}`}
                  >
                    <option value="">Authorised? —</option>
                    <option value="true">Authorised</option>
                    <option value="false">Not authorised</option>
                  </select>
                  <select
                    className={`${input} cursor-pointer py-1 text-xs`}
                    value={sponsorship === undefined || sponsorship === null ? "" : String(sponsorship)}
                    onChange={(e) =>
                      setEligibility(code, "sponsorship_required", e.target.value === "" ? null : e.target.value === "true")
                    }
                    aria-label={`Sponsorship required for ${label}`}
                  >
                    <option value="">Sponsorship? —</option>
                    <option value="true">Needs sponsorship</option>
                    <option value="false">No sponsorship</option>
                  </select>
                </div>
              );
            })}
            <p className="text-[11px] leading-relaxed text-slate-400">
              Leave a country blank if you&apos;re unsure — a blank escalates the question to
              you rather than assuming an answer.
            </p>
          </div>
        </SectionCard>
      </div>

      {/* --- answer bank --- */}
      <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card">
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-slate-900">Answer bank</h3>
            <p className="mt-0.5 text-xs text-slate-500">
              Screening questions you&apos;ve already answered. Each one is reused on later
              forms, so this gets quieter the more you use it.
            </p>
          </div>
          <div className="ml-auto flex gap-2 text-[11px]">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 font-medium text-slate-600">
              {entries.length} stored
            </span>
            {lockedCount > 0 && (
              <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-medium text-amber-700">
                {lockedCount} locked
              </span>
            )}
          </div>
        </div>

        <div className="grid gap-2 border-b border-slate-100 bg-slate-50/60 px-4 py-3 lg:grid-cols-[2fr_1.5fr_auto_auto_auto]">
          <input
            className={input}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Question, as the form asks it…"
          />
          <input
            className={input}
            value={a}
            onChange={(e) => setA(e.target.value)}
            placeholder="Your answer"
            onKeyDown={(e) => e.key === "Enter" && addAnswer()}
          />
          <select
            className={`${input} cursor-pointer`}
            value={kind}
            onChange={(e) => setKind(e.target.value as AnswerKind)}
            aria-label="Answer type"
          >
            {(["text", "boolean", "number", "select", "date"] as AnswerKind[]).map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <input
            className={`${input} min-w-[9rem]`}
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            placeholder="global"
            title="Use employer:<slug> for answers that shouldn't be reused elsewhere"
          />
          <button
            onClick={addAnswer}
            disabled={!!busy || !q.trim() || !a.trim()}
            className="rounded-lg bg-slate-900 px-3.5 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40"
          >
            {busy === "answer" ? "Saving…" : "Add"}
          </button>
        </div>

        {entries.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-slate-400">
            Nothing banked yet. Answers you add here are reused automatically on later
            applications.
          </p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {entries.map((e) => (
              <li key={e.id} className="flex flex-wrap items-start gap-2 px-4 py-2.5 transition hover:bg-slate-50">
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-slate-900">{e.question_text}</p>
                  <p className="mt-0.5 text-sm text-slate-600">{e.answer_value}</p>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                      {e.answer_kind}
                    </span>
                    {e.scope !== "global" && (
                      <span className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 font-mono text-[10px] text-indigo-700">
                        {e.scope}
                      </span>
                    )}
                    {e.locked && (
                      <span
                        className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                        title="Sensitive — reused only on an exact match, never by similarity"
                      >
                        locked
                      </span>
                    )}
                    {e.times_used > 0 && (
                      <span className="rounded border border-slate-200 px-1.5 py-0.5 text-[10px] text-slate-400">
                        used {e.times_used}×
                      </span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => removeAnswer(e.id)}
                  disabled={busy === `del-${e.id}`}
                  className="shrink-0 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-500 transition hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700 disabled:opacity-40"
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
