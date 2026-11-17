import { costByDay, cumulativeApplied, queueGroups, queueTotals } from "@/lib/dashboard-model";

test("queue groups put waiting and running states first and treat every other state as closed", () => {
  const groups = queueGroups({ Cancelled: 24, Applying: 63, "Resume Ready": 3, Queued: 167, Unavailable: 34, Empty: 0 });
  expect(groups.map((group) => [group.label, group.count, group.kind])).toEqual([
    ["Queued", 167, "queued"], ["Applying", 63, "active"], ["Resume Ready", 3, "active"], ["Cancelled", 24, "closed"], ["Unavailable", 34, "closed"],
  ]);
  expect(queueTotals(groups)).toEqual({ active: 66, queued: 167, closed: 58 });
});

test("queue groups read lowercase state keys", () => {
  expect(queueGroups({ needs_input: 1, queued: 1 }).map((group) => [group.label, group.kind])).toEqual([["Queued", "queued"], ["Needs input", "closed"]]);
});

test("cumulative applied sums days in date order", () => {
  const day = (name: string, applied: number) => ({ day: name, applied, discovered: 0, evaluated: 0 });
  expect(cumulativeApplied([day("2026-11-02", 3), day("2026-11-01", 2)])).toEqual([{ day: "2026-11-01", total: 2 }, { day: "2026-11-02", total: 5 }]);
});

test("cost by day sums runs per UTC day and fills days without runs", () => {
  const runs = [
    { started_at: "2026-11-10T08:00:00Z", total_llm_cost_usd: 0.5 },
    { started_at: "2026-11-10T20:00:00Z", total_llm_cost_usd: 0.25 },
    { started_at: "2026-11-01T20:00:00Z", total_llm_cost_usd: 9 },
  ];
  expect(costByDay(runs, 3, new Date("2026-11-11T06:00:00Z"))).toEqual([
    { day: "2026-11-09", cost: 0 }, { day: "2026-11-10", cost: 0.75 }, { day: "2026-11-11", cost: 0 },
  ]);
});
