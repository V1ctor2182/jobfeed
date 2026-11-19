import {
  formatTick, laneSegments, nodePhase, tickStep, tickValues, traceSummary,
  type ApplierTrace, type TraceGroup, type TraceStep,
} from "./model";
import { SAMPLE_APPLIER_TRACE } from "./sample-applier-trace";

function step(id: string, category: TraceStep["category"], start: number, end: number, extras: Partial<TraceStep> = {}): TraceStep {
  return { kind: "step", id, name: id, meta: "", category, start, end, outcome: "ok", ...extras };
}

function group(id: string, start: number, end: number, children: TraceGroup["children"]): TraceGroup {
  return { kind: "group", id, name: id, meta: "", role: "applier", start, end, children };
}

test("a node is pending before its start, running until its end, then done or failed by outcome", () => {
  const ok = step("ok", "browser", 5, 10);
  const failed = step("failed", "browser", 5, 10, { outcome: "failed" });
  expect(nodePhase(ok, 4.9)).toBe("pending");
  expect(nodePhase(ok, 5)).toBe("running");
  expect(nodePhase(failed, 9.9)).toBe("running");
  expect(nodePhase(ok, 10)).toBe("done");
  expect(nodePhase(failed, 10)).toBe("failed");
  expect(nodePhase(group("g", 0, 3, []), 3)).toBe("done");
});

test("a group lane shows gaps as model time, child groups as waits, and merges overlapping same-type children", () => {
  const lane = laneSegments(group("applier", 0, 20, [
    step("b", "browser", 4, 8),
    step("a", "browser", 2, 5),
    group("child", 10, 14, []),
    step("x", "backend", 14, 16, { outcome: "failed" }),
  ]));
  expect(lane).toEqual([
    { type: "model", start: 0, end: 2, failed: false },
    { type: "browser", start: 2, end: 8, failed: false },
    { type: "model", start: 8, end: 10, failed: false },
    { type: "wait", start: 10, end: 14, failed: false },
    { type: "backend", start: 14, end: 16, failed: true },
    { type: "model", start: 16, end: 20, failed: false },
  ]);
});

test("touching children of different types leave no gap segment between them", () => {
  const lane = laneSegments(group("applier", 0, 4, [step("a", "browser", 0, 2), step("b", "shell", 2, 4)]));
  expect(lane.map((segment) => segment.type)).toEqual(["browser", "shell"]);
});

const run: ApplierTrace = {
  title: "t", subtitle: "s", fieldsTotal: 5,
  root: group("worker", 0, 100, [
    step("fill", "browser", 0, 10, { fieldsFilled: 3 }),
    step("click", "browser", 10, 15, { outcome: "failed" }),
    step("think", "model", 15, 40),
    step("ask", "human", 40, 70),
    step("record", "backend", 70, 72, { fieldsFilled: 2 }),
  ]),
};

test("summary at the start counts nothing", () => {
  expect(traceSummary(run, 0)).toEqual({
    elapsed: 0, session: "working", fieldsFilled: 0, waitingOnYou: 0, toolCalls: 0, errors: 0, slowestStepId: null,
  });
});

test("summary is blocked while a human step runs and counts only finished tool calls", () => {
  expect(traceSummary(run, 50)).toEqual({
    elapsed: 50, session: "blocked", fieldsFilled: 3, waitingOnYou: 10, toolCalls: 2, errors: 1, slowestStepId: "think",
  });
});

test("summary is done at the root end, and human waits never count as the slowest step", () => {
  expect(traceSummary(run, 140)).toEqual({
    elapsed: 100, session: "done", fieldsFilled: 5, waitingOnYou: 30, toolCalls: 3, errors: 1, slowestStepId: "think",
  });
});

test("the sample run fills every field it promises and has one failure", () => {
  const summary = traceSummary(SAMPLE_APPLIER_TRACE, SAMPLE_APPLIER_TRACE.root.end);
  expect(summary.fieldsFilled).toBe(SAMPLE_APPLIER_TRACE.fieldsTotal);
  expect(summary.errors).toBe(1);
  expect(summary.session).toBe("done");
});

test("tick step keeps the axis near five ticks", () => {
  expect(tickStep(5)).toBe(1);
  expect(tickStep(60)).toBe(15);
  expect(tickStep(600)).toBe(120);
  expect(tickStep(4000)).toBe(600);
  expect(tickValues(60)).toEqual([0, 15, 30, 45, 60]);
});

test("ticks read in seconds under a minute and in minutes after", () => {
  expect(formatTick(45)).toBe("45s");
  expect(formatTick(120)).toBe("2m");
  expect(formatTick(90)).toBe("1m30");
});
