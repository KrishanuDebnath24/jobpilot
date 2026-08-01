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
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card transition-shadow hover:shadow-card-hover">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-slate-900">LLM cost</h2>
        <span className="text-2xl font-semibold tabular-nums tracking-tight text-slate-900">
          {usd(report.total_cost_usd)}
        </span>
        {report.total_savings_usd > 0 && (
          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
            saved {usd(report.total_savings_usd)} ({report.savings_pct}%) via batch + cache
          </span>
        )}
        {report.over_threshold && (
          <span
            className="ml-auto rounded-md border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700"
            title={`Days over $${report.daily_threshold_usd}/day: ${report.alerts.map((a) => a.date).join(", ")}`}
          >
            ⚠ over ${report.daily_threshold_usd}/day
          </span>
        )}
      </div>

      {agents.length === 0 ? (
        <p className="text-xs text-slate-400">No LLM spend recorded yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {agents.map(([stage, a]) => (
            <li key={stage} className="group flex items-center gap-2 text-xs">
              <span className="w-20 shrink-0 font-medium capitalize text-slate-600">{stage}</span>
              <span className="relative h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                <span
                  className="absolute inset-y-0 left-0 rounded-full bg-indigo-500 transition-all duration-500 group-hover:bg-indigo-600"
                  style={{ width: `${Math.max(3, (a.cost_usd / max) * 100)}%` }}
                />
              </span>
              <span className="w-16 shrink-0 text-right font-medium tabular-nums text-slate-700">
                {usd(a.cost_usd)}
              </span>
              <span className="w-10 shrink-0 text-right tabular-nums text-slate-400">{a.runs}×</span>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-slate-100 pt-2.5 text-[11px] text-slate-500">
        <span>in {(report.tokens.input_tokens ?? 0).toLocaleString()} tok</span>
        <span>out {(report.tokens.output_tokens ?? 0).toLocaleString()} tok</span>
        <span className={report.cache_discount_active ? "font-medium text-emerald-600" : ""}>
          cache {report.cache_discount_active ? "on" : "—"}
        </span>
        <span className={report.batch_discount_active ? "font-medium text-emerald-600" : ""}>
          batch {report.batch_discount_active ? "on" : "—"}
        </span>
      </div>
    </section>
  );
}
