"use client";

import { useEffect, useState } from "react";
import type { CostReport } from "@/lib/types";
import { getCostReport } from "@/lib/api";

const usd = (n: number) => `$${(n ?? 0).toFixed(4)}`;

/** Phase 8b: per-user LLM cost view — total spend, batch/cache savings, a breakdown by
 *  agent, and a soft daily-threshold alert. Estimates from the usage the runners log
 *  onto pipeline_runs (billing is authoritative). Read-only. */
export default function CostPanel() {
  const [report, setReport] = useState<CostReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getCostReport().then(setReport).catch((e) => setError(String(e)));
  }, []);

  if (error) return null; // cost view is informational — never block the dashboard
  if (!report) return null;

  const agents = Object.entries(report.by_agent).sort((a, b) => b[1].cost_usd - a[1].cost_usd);
  const max = Math.max(...agents.map(([, a]) => a.cost_usd), 0.000001);

  return (
    <section className="rounded-2xl border border-neutral-800 bg-neutral-900/30 p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-neutral-200">LLM cost</h2>
        <span className="text-lg font-semibold tabular-nums text-neutral-100">{usd(report.total_cost_usd)}</span>
        {report.total_savings_usd > 0 && (
          <span className="text-xs text-emerald-300/80">
            saved {usd(report.total_savings_usd)} ({report.savings_pct}%) via batch + cache
          </span>
        )}
        {report.over_threshold && (
          <span
            className="ml-auto rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300"
            title={`Days over $${report.daily_threshold_usd}/day: ${report.alerts.map((a) => a.date).join(", ")}`}
          >
            ⚠ over ${report.daily_threshold_usd}/day
          </span>
        )}
      </div>

      {agents.length === 0 ? (
        <p className="text-xs text-neutral-500">No LLM spend recorded yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {agents.map(([stage, a]) => (
            <li key={stage} className="flex items-center gap-2 text-xs">
              <span className="w-20 shrink-0 capitalize text-neutral-400">{stage}</span>
              <span className="relative h-2 flex-1 overflow-hidden rounded-full bg-neutral-800">
                <span
                  className="absolute inset-y-0 left-0 rounded-full bg-sky-500/60"
                  style={{ width: `${Math.max(3, (a.cost_usd / max) * 100)}%` }}
                />
              </span>
              <span className="w-16 shrink-0 text-right tabular-nums text-neutral-300">{usd(a.cost_usd)}</span>
              <span className="w-10 shrink-0 text-right tabular-nums text-neutral-600">{a.runs}×</span>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
        <span>in {(report.tokens.input_tokens ?? 0).toLocaleString()} tok</span>
        <span>out {(report.tokens.output_tokens ?? 0).toLocaleString()} tok</span>
        <span className={report.cache_discount_active ? "text-emerald-400/70" : ""}>
          cache {report.cache_discount_active ? "on" : "—"}
        </span>
        <span className={report.batch_discount_active ? "text-emerald-400/70" : ""}>
          batch {report.batch_discount_active ? "on" : "—"}
        </span>
      </div>
    </section>
  );
}
