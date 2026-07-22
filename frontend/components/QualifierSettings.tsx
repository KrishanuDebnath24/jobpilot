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
      <span className="text-xs font-medium text-neutral-400">{label}</span>
      {hint && <span className="block text-[11px] text-neutral-600">{hint}</span>}
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
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 px-4 py-10 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg space-y-4 rounded-2xl border border-neutral-800 bg-neutral-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-1">
          <h2 className="text-lg font-semibold tracking-tight text-neutral-100">Scoring &amp; filters</h2>
          <p className="text-sm text-neutral-500">
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
          <span className="flex items-center justify-between text-xs font-medium text-neutral-400">
            <span>Qualify threshold</span>
            <span className="tabular-nums text-neutral-300">{threshold.toFixed(2)}</span>
          </span>
          <span className="block text-[11px] text-neutral-600">
            Jobs scoring below this are marked disqualified.
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
            className="w-full accent-sky-500"
          />
        </label>

        <div className="space-y-1.5">
          <span className="flex items-center justify-between text-xs font-medium text-neutral-400">
            <span>Dimension weights</span>
            <span className="text-[11px] text-neutral-600">
              relative — renormalized{weightSum > 0 ? "" : " (add at least one)"}
            </span>
          </span>
          <div className="grid grid-cols-3 gap-2">
            {DIMENSIONS.map((d) => (
              <label key={d.key} className="space-y-1">
                <span className="block text-[11px] text-neutral-500">{d.label}</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={weights[d.key]}
                  onChange={(e) =>
                    setWeights((w) => ({ ...w, [d.key]: Math.max(0, Number(e.target.value) || 0) }))
                  }
                  className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-sm text-neutral-100 outline-none focus:border-neutral-500"
                />
              </label>
            ))}
          </div>
        </div>

        {error && <p className="text-sm text-rose-300">{error}</p>}

        <div className="flex items-center justify-end gap-3 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={!!busy}
            className="rounded-lg border border-neutral-800 px-3 py-2 text-sm text-neutral-300 transition hover:border-neutral-600 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => save(false)}
            disabled={!!busy}
            className="rounded-lg border border-neutral-700 px-3 py-2 text-sm text-neutral-200 transition hover:border-neutral-500 disabled:opacity-40"
          >
            {busy === "save" ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => save(true)}
            disabled={!!busy || weightSum <= 0}
            title={weightSum <= 0 ? "Set at least one dimension weight" : undefined}
            className="rounded-lg bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 transition hover:bg-white disabled:opacity-40"
          >
            {busy === "requalify" ? "Re-qualifying…" : "Save & re-qualify"}
          </button>
        </div>
      </div>
    </div>
  );
}
