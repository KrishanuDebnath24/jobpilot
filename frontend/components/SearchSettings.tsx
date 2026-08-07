"use client";

import { useCallback, useState } from "react";
import type { SearchConfig, SearchConfigProposal } from "@/lib/types";
import { ApiError, deriveSearchConfig, updateSearchConfig } from "@/lib/api";
import ResumeUpload from "./ResumeUpload";

export type SearchSettingsMode = "onboarding" | "edit" | "review";

// Every source is full-text and filtered to the Republic of Ireland. Adzuna, Jooble and
// Careerjet were removed: they returned teaser snippets with no reachable application
// form behind them, and Adzuna could only serve Great Britain and Northern Ireland.
const SOURCES: { id: string; label: string; note: string }[] = [
  { id: "workable", label: "Workable", note: "full-text · free · widest coverage" },
  { id: "greenhouse", label: "Greenhouse", note: "ATS · full-text · free" },
  { id: "crawl", label: "Crawler", note: "Irish board · free" },
  { id: "muse", label: "The Muse", note: "full-text · free" },
  { id: "jsearch", label: "JSearch", note: "metered — opt in" },
  { id: "apify", label: "LinkedIn (Apify)", note: "metered · read-only — opt in" },
];
// Free defaults for a brand-new user (matches the seeded set).
const DEFAULT_SOURCES = ["workable", "greenhouse", "crawl", "muse"];

/** Comma/Enter-to-add chip editor for a string list. */
function ChipInput({
  label,
  values,
  onChange,
  placeholder,
}: {
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");
  const commit = useCallback(
    (raw: string) => {
      const items = raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (items.length === 0) return;
      const seen = new Set(values.map((v) => v.toLowerCase()));
      const merged = [...values];
      for (const it of items) {
        if (!seen.has(it.toLowerCase())) {
          seen.add(it.toLowerCase());
          merged.push(it);
        }
      }
      onChange(merged);
      setDraft("");
    },
    [values, onChange],
  );

  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-semibold text-slate-700">{label}</span>
      <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2 py-1.5 transition focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-xs font-medium text-indigo-700"
          >
            {v}
            <button
              type="button"
              onClick={() => onChange(values.filter((x) => x !== v))}
              className="text-indigo-400 transition hover:text-indigo-700"
              aria-label={`Remove ${v}`}
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit(draft);
            } else if (e.key === "Backspace" && !draft && values.length) {
              onChange(values.slice(0, -1));
            }
          }}
          onBlur={() => draft && commit(draft)}
          placeholder={values.length ? "" : placeholder}
          className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-slate-900 outline-none placeholder:text-slate-400"
        />
      </div>
    </label>
  );
}

export default function SearchSettings({
  mode,
  initial,
  proposal,
  onClose,
  onSaved,
}: {
  mode: SearchSettingsMode;
  initial: SearchConfig | null;
  proposal?: SearchConfigProposal | null;
  onClose: () => void;
  onSaved: (config: SearchConfig) => void;
}) {
  // In review mode the LLM proposal overwrites the derived fields (roles/keywords/
  // locations); user-owned fields (sources/target_employers) come from the current config.
  const [roles, setRoles] = useState<string[]>(proposal?.roles ?? initial?.roles ?? []);
  const [keywords, setKeywords] = useState<string[]>(proposal?.keywords ?? initial?.keywords ?? []);
  const [locations, setLocations] = useState<string[]>(
    (proposal?.locations?.length ? proposal.locations : initial?.locations) ?? [],
  );
  const [sources, setSources] = useState<string[]>(
    initial?.sources?.length ? initial.sources : DEFAULT_SOURCES,
  );
  const [employers, setEmployers] = useState<string[]>(initial?.target_employers ?? []);
  const [hint, setHint] = useState("");
  const [notes, setNotes] = useState<string | null>(proposal?.notes ?? null);
  const [busy, setBusy] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blocking = mode === "onboarding";

  const suggest = useCallback(async () => {
    setSuggesting(true);
    setError(null);
    try {
      const { proposal: p, has_resume } = await deriveSearchConfig(hint);
      if (!has_resume) {
        setError("No résumé on file yet — attach a CV first, then suggest.");
        return;
      }
      setRoles(p.roles);
      setKeywords(p.keywords);
      if (p.locations.length) setLocations(p.locations);
      setNotes(p.notes ?? null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSuggesting(false);
    }
  }, [hint]);

  const save = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const config = await updateSearchConfig({
        roles,
        locations,
        keywords,
        sources,
        target_employers: employers,
      });
      onSaved(config);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [roles, locations, keywords, sources, employers, onSaved]);

  const title =
    mode === "onboarding"
      ? "Set up your job search"
      : mode === "review"
        ? "Review search config from your résumé"
        : "Edit search config";
  const subtitle =
    mode === "onboarding"
      ? "This is what Scout searches for. You can refine it any time."
      : mode === "review"
        ? "We studied your résumé and proposed market-standard roles. Edit anything, then save."
        : "Roles, locations, keywords, and which sources Scout polls.";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/25 px-4 py-10 backdrop-blur-sm"
      onClick={() => !blocking && onClose()}
    >
      <div
        className="screen-enter w-full max-w-lg space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl shadow-slate-900/10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h2>
          <p className="text-sm leading-relaxed text-slate-500">{subtitle}</p>
        </div>

        {mode === "onboarding" && (
          <div className="space-y-2">
            <p className="text-xs leading-relaxed text-slate-500">
              Attach your CV and we&apos;ll propose market-standard roles from it — then edit
              and save below. Or just fill the fields in yourself.
            </p>
            {/* Upload here auto-runs the proposal, so onboarding is CV → suggest → confirm
                without leaving this modal. */}
            <ResumeUpload onUploaded={() => suggest()} />
          </div>
        )}

        <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-[12rem] flex-1 space-y-1.5">
              <span className="text-xs font-semibold text-slate-700">
                Target hint <span className="font-normal text-slate-400">(optional)</span>
              </span>
              <input
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                placeholder="e.g. senior backend / platform roles"
                className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
              />
            </label>
            <button
              type="button"
              onClick={suggest}
              disabled={suggesting || busy}
              className="flex items-center gap-2 rounded-lg bg-sky-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-sky-700 active:scale-[0.98] disabled:opacity-40 disabled:hover:bg-sky-600"
            >
              {suggesting && (
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
              )}
              {suggesting ? "Studying résumé…" : "Suggest from résumé"}
            </button>
          </div>
          {notes && <p className="mt-2 text-xs text-slate-600">{notes}</p>}
        </div>

        <ChipInput label="Roles" values={roles} onChange={setRoles} placeholder="Software Engineer, Backend Engineer…" />
        <ChipInput label="Locations" values={locations} onChange={setLocations} placeholder="Dublin, Belfast…" />
        <ChipInput label="Keywords / skills" values={keywords} onChange={setKeywords} placeholder="python, react…" />
        <ChipInput
          label="Target employers (Greenhouse slugs)"
          values={employers}
          onChange={setEmployers}
          placeholder="intercom, stripe…"
        />

        <div className="space-y-1.5">
          <span className="text-xs font-semibold text-slate-700">Sources</span>
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {SOURCES.map((s) => {
              const on = sources.includes(s.id);
              return (
                <label
                  key={s.id}
                  className={`flex cursor-pointer items-start gap-2 rounded-lg border px-2 py-1.5 text-xs transition ${
                    on
                      ? "border-sky-300 bg-sky-50 text-sky-900 shadow-card"
                      : "border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={(e) =>
                      setSources((prev) =>
                        e.target.checked ? [...prev, s.id] : prev.filter((x) => x !== s.id),
                      )
                    }
                    className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-slate-300 accent-sky-600"
                  />
                  <span>
                    <span className="block font-semibold">{s.label}</span>
                    <span className={on ? "text-sky-700/70" : "text-slate-400"}>{s.note}</span>
                  </span>
                </label>
              );
            })}
          </div>
        </div>

        {error && (
          <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        <div className="flex items-center justify-end gap-3 pt-1">
          {!blocking && (
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-lg px-3 py-2 text-sm font-medium text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          <button
            type="button"
            onClick={save}
            disabled={busy || roles.length === 0}
            title={roles.length === 0 ? "Add at least one role" : undefined}
            className="flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40"
          >
            {busy && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            )}
            {busy ? "Saving…" : mode === "onboarding" ? "Save & continue" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
