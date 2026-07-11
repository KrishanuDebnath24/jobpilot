"use client";

import { useCallback, useState } from "react";
import type { SearchConfig, SearchConfigProposal } from "@/lib/types";
import { ApiError, deriveSearchConfig, updateSearchConfig } from "@/lib/api";
import ResumeUpload from "./ResumeUpload";

export type SearchSettingsMode = "onboarding" | "edit" | "review";

const SOURCES: { id: string; label: string; note: string }[] = [
  { id: "greenhouse", label: "Greenhouse", note: "ATS · full-text · free" },
  { id: "crawl", label: "Crawler", note: "Irish board · free" },
  { id: "jooble", label: "Jooble", note: "free" },
  { id: "careerjet", label: "Careerjet", note: "free" },
  { id: "muse", label: "The Muse", note: "full-text · free" },
  { id: "adzuna", label: "Adzuna", note: "NI coverage" },
  { id: "jsearch", label: "JSearch", note: "metered — opt in" },
];
// Sensible free defaults for a brand-new user (matches the seeded default set minus Adzuna).
const DEFAULT_SOURCES = ["greenhouse", "crawl", "jooble", "careerjet", "muse"];

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
      <span className="text-xs font-medium text-neutral-400">{label}</span>
      <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded-md border border-neutral-700 bg-neutral-800 px-1.5 py-0.5 text-xs text-neutral-200"
          >
            {v}
            <button
              type="button"
              onClick={() => onChange(values.filter((x) => x !== v))}
              className="text-neutral-500 hover:text-neutral-200"
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
          className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-neutral-100 outline-none placeholder:text-neutral-600"
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
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 px-4 py-10 backdrop-blur-sm"
      onClick={() => !blocking && onClose()}
    >
      <div
        className="w-full max-w-lg space-y-4 rounded-2xl border border-neutral-800 bg-neutral-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight text-neutral-100">{title}</h2>
          <p className="text-sm text-neutral-500">{subtitle}</p>
        </div>

        {mode === "onboarding" && (
          <div className="space-y-2">
            <p className="text-xs text-neutral-500">
              Attach your CV and we&apos;ll propose market-standard roles from it — then edit
              and save below. Or just fill the fields in yourself.
            </p>
            {/* Upload here auto-runs the proposal, so onboarding is CV → suggest → confirm
                without leaving this modal. */}
            <ResumeUpload onUploaded={() => suggest()} />
          </div>
        )}

        <div className="rounded-lg border border-neutral-800 bg-neutral-950/50 p-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-[12rem] flex-1 space-y-1.5">
              <span className="text-xs font-medium text-neutral-400">
                Target hint <span className="text-neutral-600">(optional)</span>
              </span>
              <input
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                placeholder="e.g. senior backend / platform roles"
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-100 outline-none focus:border-neutral-500"
              />
            </label>
            <button
              type="button"
              onClick={suggest}
              disabled={suggesting || busy}
              className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200 transition hover:bg-sky-500/20 disabled:opacity-40"
            >
              {suggesting ? "Studying résumé…" : "Suggest from résumé"}
            </button>
          </div>
          {notes && <p className="mt-2 text-xs text-neutral-400">{notes}</p>}
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
          <span className="text-xs font-medium text-neutral-400">Sources</span>
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {SOURCES.map((s) => {
              const on = sources.includes(s.id);
              return (
                <label
                  key={s.id}
                  className={`flex cursor-pointer items-start gap-2 rounded-lg border px-2 py-1.5 text-xs transition ${
                    on
                      ? "border-sky-500/40 bg-sky-500/10 text-sky-100"
                      : "border-neutral-800 bg-neutral-950/40 text-neutral-400 hover:border-neutral-700"
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
                    className="mt-0.5 accent-sky-500"
                  />
                  <span>
                    <span className="block font-medium">{s.label}</span>
                    <span className="text-neutral-500">{s.note}</span>
                  </span>
                </label>
              );
            })}
          </div>
        </div>

        {error && <p className="text-sm text-rose-300">{error}</p>}

        <div className="flex items-center justify-end gap-3 pt-1">
          {!blocking && (
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="rounded-lg border border-neutral-800 px-3 py-2 text-sm text-neutral-300 transition hover:border-neutral-600 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          <button
            type="button"
            onClick={save}
            disabled={busy || roles.length === 0}
            title={roles.length === 0 ? "Add at least one role" : undefined}
            className="rounded-lg bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 transition hover:bg-white disabled:opacity-40"
          >
            {busy ? "Saving…" : mode === "onboarding" ? "Save & continue" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
