"use client";

import { useCallback, useMemo, useState } from "react";
import type { QualifierConfig, QualifierWeights } from "@/lib/types";
import { ApiError, runQualify, updateQualifierConfig } from "@/lib/api";

const DIMENSIONS: { key: keyof QualifierWeights; label: string }[] = [
  { key: "skills", label: "Skills" },
  { key: "experience", label: "Experience" },
  { key: "roles_match", label: "Roles match" },
];

/** Comma/Enter-to-add chip editor for a string list (self-contained; mirrors SearchSettings). */
function ChipInput({
  label,
  hint,
  values,
  onChange,
  placeholder,
}: {
  label: string;
  hint?: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");
  const commit = useCallback(
    (raw: string) => {
      const items = raw.split(",").map((s) => s.trim()).filter(Boolean);
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
      {hint && <span className="block text-[11px] leading-relaxed text-slate-400">{hint}</span>}
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

const DEFAULT_WEIGHTS: Required<QualifierWeights> = { skills: 0.5, experience: 0.2, roles_match: 0.3 };

export default function QualifierSettings({
  initial,
  onClose,
  onSaved,
}: {
  initial: QualifierConfig | null;
  onClose: () => void;
  onSaved: (config: QualifierConfig) => void;
}) {
  const [mustHave, setMustHave] = useState<string[]>(initial?.must_have ?? []);
  const [niceToHave, setNiceToHave] = useState<string[]>(initial?.nice_to_have ?? []);
  const [threshold, setThreshold] = useState<number>(
    typeof initial?.threshold === "number" ? initial.threshold : 0.5,
  );
  const [weights, setWeights] = useState<Required<QualifierWeights>>({
    ...DEFAULT_WEIGHTS,
    ...(initial?.weights_json ?? {}),
  });
  const [busy, setBusy] = useState<null | "save" | "requalify">(null);
  const [error, setError] = useState<string | null>(null);

  const weightSum = useMemo(
    () => DIMENSIONS.reduce((s, d) => s + (Number(weights[d.key]) || 0), 0),
    [weights],
  );

  const save = useCallback(
    async (requalify: boolean) => {
      setBusy(requalify ? "requalify" : "save");
      setError(null);
      try {
        const config = await updateQualifierConfig({
          must_have: mustHave,
          nice_to_have: niceToHave,
          threshold,
          weights_json: weights,
        });
        if (requalify) await runQualify(true); // re-score the whole pool against the new config
        onSaved(config);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [mustHave, niceToHave, threshold, weights, onSaved],
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/25 px-4 py-10 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="screen-enter w-full max-w-lg space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl shadow-slate-900/10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight text-slate-900">Scoring &amp; filters</h2>
          <p className="text-sm leading-relaxed text-slate-500">
            How Qualify scores each job, plus the must-have hard filter. Changes apply on the next
            Qualify run — or re-qualify now.
          </p>
        </div>

        <ChipInput
          label="Must-have terms (hard filter)"
          hint="A job whose text is missing ANY of these is disqualified. e.g. “contract”, “senior”, “remote”. All terms are required (AND)."
          values={mustHave}
          onChange={setMustHave}
          placeholder="contract, senior…"
        />
        <ChipInput
          label="Nice-to-have skills"
          hint="Soft signals that inform scoring (never disqualify)."
          values={niceToHave}
          onChange={setNiceToHave}
          placeholder="python, kubernetes…"
        />

        <label className="block space-y-1.5">
          <span className="flex items-center justify-between text-xs font-semibold text-slate-700">
            <span>Qualify threshold</span>
            <span className="rounded-md bg-indigo-50 px-2 py-0.5 tabular-nums text-indigo-700">
              {threshold.toFixed(2)}
            </span>
          </span>
          <span className="block text-[11px] text-slate-400">
            Jobs scoring below this are marked disqualified.
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
            className="w-full cursor-pointer accent-indigo-600"
          />
        </label>

        <div className="space-y-1.5">
          <span className="flex items-center justify-between text-xs font-semibold text-slate-700">
            <span>Dimension weights</span>
            <span className="text-[11px] font-normal text-slate-400">
              relative — renormalized{weightSum > 0 ? "" : " (add at least one)"}
            </span>
          </span>
          <div className="grid grid-cols-3 gap-2">
            {DIMENSIONS.map((d) => (
              <label key={d.key} className="space-y-1">
                <span className="block text-[11px] text-slate-500">{d.label}</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={weights[d.key]}
                  onChange={(e) =>
                    setWeights((w) => ({ ...w, [d.key]: Math.max(0, Number(e.target.value) || 0) }))
                  }
                  className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-sm text-slate-900 outline-none transition focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
            ))}
          </div>
        </div>

        {error && (
          <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        <div className="flex items-center justify-end gap-3 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={!!busy}
            className="rounded-lg px-3 py-2 text-sm font-medium text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => save(false)}
            disabled={!!busy}
            className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50 active:scale-[0.98] disabled:opacity-40"
          >
            {busy === "save" ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => save(true)}
            disabled={!!busy || weightSum <= 0}
            title={weightSum <= 0 ? "Set at least one dimension weight" : undefined}
            className="flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40"
          >
            {busy === "requalify" && (
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            )}
            {busy === "requalify" ? "Re-qualifying…" : "Save & re-qualify"}
          </button>
        </div>
      </div>
    </div>
  );
}
