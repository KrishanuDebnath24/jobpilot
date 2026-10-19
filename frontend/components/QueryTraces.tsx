"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { QueryTrace, TraceSpan, TraceSummary, TraceToolRollup } from "@/lib/types";
import { getQueryTraces } from "@/lib/api";

/** Two categorical hues, and only two: indigo is time spent inside the model, cyan is
 *  time spent inside a tool. The pair was checked for colour-vision separation (ΔE 18.5
 *  deutan, 22.4 normal) rather than eyeballed. Rose is reserved for status — an errored
 *  span turns rose AND says "error", so state never rests on colour alone. */
const LLM_FILL = "bg-indigo-500";
const TOOL_FILL = "bg-cyan-500";
const ERR_FILL = "bg-rose-500";

const WINDOWS: { label: string; days: number | null }[] = [
  { label: "24 hours", days: 1 },
  { label: "7 days", days: 7 },
  { label: "30 days", days: 30 },
  { label: "All time", days: null },
];

type RowFilter = "all" | "errors" | "ungrounded";

// --------------------------------------------------------------------------- //
// Formatting
// --------------------------------------------------------------------------- //
function ms(v: number | null | undefined): string {
  if (v == null) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}s` : `${Math.round(v)}ms`;
}

function usd(v: number | null | undefined): string {
  if (!v) return "$0";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 24 * 60) return `${Math.round(mins / 60)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Where a turn's wall clock actually went. The tools of one round run concurrently, so
 *  their contribution is the SLOWEST of each round, not the sum — adding them would
 *  report more elapsed time than the turn took. */
function split(trace: QueryTrace): { model: number; tools: number; other: number } {
  const model = trace.spans.filter((s) => s.kind === "llm").reduce((n, s) => n + s.ms, 0);
  const rounds = new Map<number, number>();
  for (const s of trace.spans) {
    if (s.kind !== "tool") continue;
    rounds.set(s.t, Math.max(rounds.get(s.t) ?? 0, s.ms));
  }
  const tools = [...rounds.values()].reduce((n, v) => n + v, 0);
  return { model, tools, other: Math.max(0, trace.duration_ms - model - tools) };
}

// --------------------------------------------------------------------------- //
// Pieces
// --------------------------------------------------------------------------- //
function Tile({
  label,
  value,
  sub,
  tone = "slate",
}: {
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: "slate" | "rose" | "emerald";
}) {
  const valueTone =
    tone === "rose" ? "text-rose-600" : tone === "emerald" ? "text-emerald-600" : "text-slate-900";
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-3 shadow-card">
      <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p className={`mt-1 text-2xl font-semibold leading-none ${valueTone}`}>{value}</p>
      {sub && <p className="mt-1.5 text-[11px] leading-tight text-slate-500">{sub}</p>}
    </div>
  );
}

/** Cache-hit meter. The unfilled track is a lighter step of the same hue, so the state
 *  reads across the whole bar rather than only where it is filled. */
function Meter({ pct }: { pct: number }) {
  return (
    <span className="mt-1.5 flex h-1.5 w-full overflow-hidden rounded-full bg-indigo-100">
      <span className="h-full rounded-full bg-indigo-500" style={{ width: `${Math.min(100, pct)}%` }} />
    </span>
  );
}

function StatusPill({ trace }: { trace: QueryTrace }) {
  if (trace.status === "error")
    return (
      <span className="rounded-md bg-rose-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-700">
        failed
      </span>
    );
  if (trace.tool_errors > 0)
    return (
      <span className="rounded-md bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
        {trace.tool_errors} tool error{trace.tool_errors > 1 ? "s" : ""}
      </span>
    );
  if (trace.tools.length === 0)
    return (
      <span
        className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500"
        title="Answered without calling a tool — from the conversation alone, not from your data"
      >
        no tools
      </span>
    );
  return null;
}

/** The waterfall: every span placed by its offset and drawn to its own duration. The
 *  tools of a round share a start, so they overlap here — which is the point, and the
 *  only honest way to show that three reads cost one round trip. */
function Waterfall({ trace }: { trace: QueryTrace }) {
  const scale = Math.max(trace.duration_ms, ...trace.spans.map((s) => s.t + s.ms), 1);
  return (
    <div className="mt-2 space-y-1">
      {trace.spans.map((span, i) => {
        const errored = span.status === "error";
        const fill = errored ? ERR_FILL : span.kind === "llm" ? LLM_FILL : TOOL_FILL;
        return (
          <div key={i} className="grid grid-cols-[10rem_1fr_3.5rem] items-center gap-2">
            <span
              className={`truncate font-mono text-[11px] ${
                span.kind === "llm" ? "text-slate-500" : "text-slate-700"
              }`}
              title={span.kind === "tool" ? span.args : (span.stop_reason ?? undefined)}
            >
              {span.name}
            </span>
            <span className="relative block h-3 rounded bg-slate-100">
              <span
                className={`absolute top-0 h-3 rounded-[3px] ${fill}`}
                style={{
                  left: `${(span.t / scale) * 100}%`,
                  width: `${Math.max((span.ms / scale) * 100, 0.7)}%`,
                }}
              />
            </span>
            <span className="text-right font-mono text-[11px] tabular-nums text-slate-500">
              {ms(span.ms)}
            </span>
          </div>
        );
      })}
      <div className="grid grid-cols-[10rem_1fr_3.5rem] gap-2 pt-0.5">
        <span />
        <span className="flex justify-between border-t border-slate-200 pt-0.5 text-[10px] tabular-nums text-slate-400">
          <span>0</span>
          <span>{ms(scale)}</span>
        </span>
        <span />
      </div>
    </div>
  );
}

/** Everything the spans carry that a bar can't: what a tool was asked, what it returned,
 *  and what each round cost in tokens. */
function SpanDetail({ spans }: { spans: TraceSpan[] }) {
  return (
    <div className="mt-3 space-y-1.5">
      {spans.map((span, i) => (
        <div key={i} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]">
          <span className={`h-2 w-2 shrink-0 rounded-[2px] ${span.kind === "llm" ? LLM_FILL : TOOL_FILL}`} />
          <span className="font-mono text-slate-600">{span.name}</span>
          {span.kind === "tool" ? (
            <>
              {span.args && span.args !== "{}" && (
                <code className="rounded bg-slate-100 px-1 py-px font-mono text-[10px] text-slate-600">
                  {span.args}
                </code>
              )}
              <span className="text-slate-500">
                {span.rows != null ? `${span.rows} rows` : "no rows"}
                {span.bytes != null ? ` · ${compact(span.bytes)}B` : ""}
              </span>
              {span.error && <span className="text-rose-600">error: {span.error}</span>}
            </>
          ) : (
            <span className="text-slate-500">
              {span.ttft_ms != null ? `first token ${ms(span.ttft_ms)} · ` : ""}
              {compact(span.input_tokens ?? 0)} in
              {span.cache_read_input_tokens
                ? ` (+${compact(span.cache_read_input_tokens)} cached)`
                : ""}{" "}
              · {compact(span.output_tokens ?? 0)} out
              {span.stop_reason ? ` · ${span.stop_reason}` : ""}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

/** Tool leaderboard. One series (calls), so one hue and no legend — the row label names
 *  it. Every bar is also read as a number, so nothing depends on the bar alone. */
function ToolTable({ tools }: { tools: TraceToolRollup[] }) {
  const max = Math.max(1, ...tools.map((t) => t.calls));
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left text-xs">
        <thead>
          <tr className="border-b border-slate-200 text-[11px] font-medium text-slate-400">
            <th className="py-1.5 pr-2 font-medium">Tool</th>
            <th className="py-1.5 pr-2 font-medium">Calls</th>
            <th className="py-1.5 pr-2 text-right font-medium">p50</th>
            <th className="py-1.5 pr-2 text-right font-medium">p95</th>
            <th className="py-1.5 pr-2 text-right font-medium">Avg rows</th>
            <th className="py-1.5 text-right font-medium">Errors</th>
          </tr>
        </thead>
        <tbody>
          {tools.map((t) => (
            <tr key={t.tool} className="border-b border-slate-100 last:border-0">
              <td className="py-1.5 pr-2 font-mono text-[11px] text-slate-700">{t.tool}</td>
              <td className="py-1.5 pr-2">
                <span className="flex items-center gap-2">
                  <span className="h-2.5 w-24 overflow-hidden rounded-sm bg-slate-100">
                    <span
                      className={`block h-full rounded-[3px] ${TOOL_FILL}`}
                      style={{ width: `${(t.calls / max) * 100}%` }}
                    />
                  </span>
                  <span className="tabular-nums text-slate-600">{t.calls}</span>
                </span>
              </td>
              <td className="py-1.5 pr-2 text-right tabular-nums text-slate-600">{ms(t.p50_ms)}</td>
              <td className="py-1.5 pr-2 text-right tabular-nums text-slate-500">{ms(t.p95_ms)}</td>
              <td className="py-1.5 pr-2 text-right tabular-nums text-slate-500">
                {t.avg_rows ?? "—"}
              </td>
              <td
                className={`py-1.5 text-right tabular-nums ${
                  t.errors ? "font-medium text-rose-600" : "text-slate-400"
                }`}
              >
                {t.errors ? `${t.errors} (${t.error_pct}%)` : "0"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TraceRow({
  trace,
  widest,
  open,
  onToggle,
}: {
  trace: QueryTrace;
  widest: number;
  open: boolean;
  onToggle: () => void;
}) {
  const parts = split(trace);
  const w = (v: number) => `${(v / widest) * 100}%`;
  return (
    <li className="border-b border-slate-100 last:border-0">
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full flex-col gap-1.5 px-3 py-2.5 text-left transition hover:bg-slate-50"
      >
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="truncate text-sm font-medium text-slate-800">{trace.question}</span>
          <StatusPill trace={trace} />
          <span className="ml-auto shrink-0 text-[11px] tabular-nums text-slate-400">
            {when(trace.created_at ?? trace.started_at)}
          </span>
        </span>
        <span className="flex items-center gap-2">
          {/* One scale across every row, so a slow turn looks slow next to a fast one.
              The 2px surface gaps — not borders — are what separate the segments. */}
          <span className="flex h-2 flex-1 items-center overflow-hidden rounded-sm bg-slate-100">
            <span className={`h-full rounded-l-[3px] ${LLM_FILL}`} style={{ width: w(parts.model) }} />
            <span className="h-full w-[2px] shrink-0 bg-white" />
            <span className={`h-full ${TOOL_FILL}`} style={{ width: w(parts.tools) }} />
            <span className="h-full w-[2px] shrink-0 bg-white" />
            <span className="h-full rounded-r-[3px] bg-slate-300" style={{ width: w(parts.other) }} />
          </span>
          <span className="w-14 shrink-0 text-right font-mono text-[11px] tabular-nums text-slate-600">
            {ms(trace.duration_ms)}
          </span>
          <span className="w-16 shrink-0 text-right font-mono text-[11px] tabular-nums text-slate-400">
            {usd(trace.usage?.estimated_cost_usd)}
          </span>
        </span>
        <span className="flex flex-wrap items-center gap-1">
          {trace.tools.length === 0 && (
            <span className="text-[11px] text-slate-400">answered from the conversation</span>
          )}
          {trace.tools.map((t, i) => (
            <span
              key={`${t}-${i}`}
              className="rounded border border-slate-200 bg-white px-1.5 py-px font-mono text-[10px] text-slate-500"
            >
              {t}
            </span>
          ))}
          <span className="ml-auto text-[11px] text-slate-400">
            {trace.rounds} round{trace.rounds === 1 ? "" : "s"} · {trace.citations} cited
          </span>
        </span>
      </button>

      {open && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-3 py-3">
          <Waterfall trace={trace} />
          <SpanDetail spans={trace.spans} />
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-4">
            <div>
              <dt className="text-slate-400">First token</dt>
              <dd className="tabular-nums text-slate-700">{ms(trace.ttft_ms)}</dd>
            </div>
            <div>
              <dt className="text-slate-400">Tokens</dt>
              <dd className="tabular-nums text-slate-700">
                {compact(trace.usage?.input_tokens ?? 0)} in ·{" "}
                {compact(trace.usage?.output_tokens ?? 0)} out
              </dd>
            </div>
            <div>
              <dt className="text-slate-400">Cached input</dt>
              <dd className="tabular-nums text-slate-700">
                {compact(trace.usage?.cache_read_input_tokens ?? 0)}
              </dd>
            </div>
            <div>
              <dt className="text-slate-400">Model</dt>
              <dd className="truncate font-mono text-[10px] text-slate-700">{trace.model}</dd>
            </div>
          </dl>
          {trace.error && (
            <p className="mt-2 rounded-lg border border-rose-100 bg-rose-50 px-2 py-1.5 text-[11px] text-rose-700">
              {trace.error}
            </p>
          )}
          {trace.answer_preview && (
            <p className="mt-2 border-l-2 border-slate-200 pl-2 text-[11px] leading-relaxed text-slate-500">
              {trace.answer_preview}
              {trace.answer_chars > trace.answer_preview.length ? "…" : ""}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

// --------------------------------------------------------------------------- //
// The screen
// --------------------------------------------------------------------------- //
/** Tracing for the Query agent: one row per answered turn, with the spans, tokens and
 *  cost behind it. Answers it exists to give — why was that turn slow, did it actually
 *  read the data or answer from memory, which tool is failing, what is a question
 *  costing. Read-only over ``agent_events``; nothing here can re-run a turn. */
export default function QueryTraces() {
  const [days, setDays] = useState<number | null>(7);
  const [rowFilter, setRowFilter] = useState<RowFilter>("all");
  const [traces, setTraces] = useState<QueryTrace[]>([]);
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (first: boolean) => {
      first ? setLoading(true) : setRefreshing(true);
      try {
        const report = await getQueryTraces(days, 200);
        setTraces(report.traces);
        setSummary(report.summary);
        setError(null);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [days],
  );

  useEffect(() => {
    load(true);
  }, [load]);

  // Poll while the tab is in front. A trace appears the moment a turn ends, so a stale
  // dashboard is the one thing this screen must not be.
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") load(false);
    }, 30000);
    return () => clearInterval(id);
  }, [load]);

  const rows = useMemo(() => {
    if (rowFilter === "errors")
      return traces.filter((t) => t.status === "error" || t.tool_errors > 0);
    if (rowFilter === "ungrounded") return traces.filter((t) => t.tools.length === 0);
    return traces;
  }, [traces, rowFilter]);

  const widest = useMemo(
    () => Math.max(1, ...rows.map((t) => t.duration_ms)),
    [rows],
  );

  const selectClass =
    "cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-700 shadow-card transition hover:border-slate-300 focus:border-indigo-400 focus:outline-none focus:ring-2 focus:ring-indigo-100";

  return (
    <div className={`space-y-4 transition-opacity ${refreshing ? "opacity-60" : "opacity-100"}`}>
      {/* One filter row above everything it scopes — the window drives the tiles, the
          tool rollup and the list alike. */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={days ?? "all"}
          onChange={(e) => setDays(e.target.value === "all" ? null : Number(e.target.value))}
          className={selectClass}
          aria-label="Time window"
        >
          {WINDOWS.map((w) => (
            <option key={w.label} value={w.days ?? "all"}>
              {w.label}
            </option>
          ))}
        </select>
        <select
          value={rowFilter}
          onChange={(e) => setRowFilter(e.target.value as RowFilter)}
          className={selectClass}
          aria-label="Filter turns"
        >
          <option value="all">All turns</option>
          <option value="errors">Failures only</option>
          <option value="ungrounded">No tool call</option>
        </select>
        <button
          onClick={() => load(false)}
          disabled={refreshing}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-600 shadow-card transition hover:border-slate-300 hover:text-slate-900 disabled:opacity-50"
        >
          Refresh
        </button>
        <span className="ml-auto flex items-center gap-3 text-[11px] text-slate-500">
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-[2px] ${LLM_FILL}`} /> model
          </span>
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-[2px] ${TOOL_FILL}`} /> tools
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-[2px] bg-slate-300" /> other
          </span>
        </span>
      </div>

      {error && (
        <p className="rounded-xl border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      {loading ? (
        <div className="grid h-40 place-items-center rounded-2xl border border-slate-200 bg-white shadow-card">
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-200 border-t-indigo-600" />
        </div>
      ) : summary && summary.turns === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white px-5 py-10 text-center shadow-card">
          <p className="text-sm font-medium text-slate-700">No traced turns in this window</p>
          <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-slate-500">
            Every question you ask in <span className="font-medium">Ask about your data</span> is
            traced from here on — the tools it called, how long each took, what they returned, and
            what the turn cost.
          </p>
        </div>
      ) : (
        summary && (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
              <Tile
                label="Turns"
                value={String(summary.turns)}
                sub={
                  summary.errors > 0 ? (
                    <span className="text-rose-600">{summary.errors} failed</span>
                  ) : (
                    "all completed"
                  )
                }
              />
              <Tile
                label="Turn time"
                value={ms(summary.latency_ms.p50)}
                sub={`p95 ${ms(summary.latency_ms.p95)} · max ${ms(summary.latency_ms.max)}`}
              />
              <Tile
                label="First token"
                value={ms(summary.ttft_ms.p50)}
                sub={`p95 ${ms(summary.ttft_ms.p95)}`}
              />
              <Tile
                label="Cost"
                value={usd(summary.cost_usd)}
                sub={`${usd(summary.cost_per_turn_usd)} per turn`}
              />
              <Tile
                label="Cache hit"
                value={`${summary.cache_hit_pct}%`}
                sub={
                  <>
                    <Meter pct={summary.cache_hit_pct} />
                    <span className="mt-1 block">
                      saves {summary.savings_pct}% vs uncached
                    </span>
                  </>
                }
              />
              <Tile
                label="Grounded"
                value={`${summary.turns - summary.ungrounded_turns}/${summary.turns}`}
                tone={summary.ungrounded_turns > 0 ? "rose" : "emerald"}
                sub={`${summary.uncited_turns} returned rows but cited none`}
              />
            </div>

            {summary.tools.length > 0 && (
              <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card">
                <h3 className="text-sm font-semibold text-slate-900">Tools</h3>
                <p className="mb-2 mt-0.5 text-xs text-slate-500">
                  Which reads the agent actually makes, how long each takes, and how much it
                  brings back.
                </p>
                <ToolTable tools={summary.tools} />
              </section>
            )}

            <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card">
              <div className="flex items-baseline gap-2 border-b border-slate-100 px-4 py-3">
                <h3 className="text-sm font-semibold text-slate-900">Turns</h3>
                <p className="text-xs text-slate-500">Newest first — open one for its waterfall.</p>
                <span className="ml-auto text-[11px] tabular-nums text-slate-400">
                  {rows.length} of {traces.length}
                </span>
              </div>
              {rows.length === 0 ? (
                <p className="px-4 py-8 text-center text-xs text-slate-400">
                  No turns match this filter.
                </p>
              ) : (
                <ul>
                  {rows.map((t) => (
                    <TraceRow
                      key={t.id}
                      trace={t}
                      widest={widest}
                      open={openId === t.id}
                      onToggle={() => setOpenId(openId === t.id ? null : t.id)}
                    />
                  ))}
                </ul>
              )}
            </section>
          </>
        )
      )}
    </div>
  );
}
