/** Pure shaping for the Dashboard page. Inputs are structural twins of the API payloads. */
import type { DailyPoint } from "@/lib/daily-series";

export type DotKind = "active" | "queued" | "closed";
export interface QueueGroup { label: string; count: number; kind: DotKind }

const QUEUE_ORDER = ["queued", "applying", "resume_ready"];
const KINDS: Record<string, DotKind> = { queued: "queued", applying: "active", resume_ready: "active" };

function stateKey(state: string): string {
  return state.trim().toLowerCase().replace(/\s+/g, "_");
}

/** One group per queue state: waiting and running states first, every other state after them as closed. */
export function queueGroups(counts: Record<string, number>): QueueGroup[] {
  const rank = (state: string) => {
    const index = QUEUE_ORDER.indexOf(stateKey(state));
    return index === -1 ? QUEUE_ORDER.length : index;
  };
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([state, count]) => {
      const text = state.replace(/_/g, " ");
      return { label: text.charAt(0).toUpperCase() + text.slice(1), count, kind: KINDS[stateKey(state)] ?? "closed" };
    });
}

export function queueTotals(groups: readonly QueueGroup[]): Record<DotKind, number> {
  const totals: Record<DotKind, number> = { active: 0, queued: 0, closed: 0 };
  for (const group of groups) totals[group.kind] += group.count;
  return totals;
}

/** Running total of applications per day, oldest first. */
export function cumulativeApplied(daily: readonly DailyPoint[]): { day: string; total: number }[] {
  let total = 0;
  return [...daily].sort((a, b) => a.day.localeCompare(b.day)).map((entry) => ({ day: entry.day, total: (total += entry.applied) }));
}

/** Model cost per UTC day from run summaries, for the trailing `days` ending today. */
export function costByDay(runs: readonly { started_at: string; total_llm_cost_usd: number }[], days: number, now: Date = new Date()): { day: string; cost: number }[] {
  const byDay = new Map<string, number>();
  for (const run of runs) {
    const day = run.started_at.slice(0, 10);
    byDay.set(day, (byDay.get(day) ?? 0) + run.total_llm_cost_usd);
  }
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return Array.from({ length: days }, (_, index) => {
    const day = new Date(today - (days - 1 - index) * 86_400_000).toISOString().slice(0, 10);
    return { day, cost: byDay.get(day) ?? 0 };
  });
}
