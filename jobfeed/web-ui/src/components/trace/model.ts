/** Applier trace model: a tree of timed spans, and pure reads of it at a moment. */

export type StepCategory = "model" | "browser" | "backend" | "shell" | "human";
export type GroupRole = "worker" | "applier";

/** Times are seconds from run start. */
export interface TraceGroup {
  kind: "group";
  id: string;
  name: string;
  meta: string;
  role: GroupRole;
  start: number;
  end: number;
  children: TraceNode[];
}

export interface TraceStep {
  kind: "step";
  id: string;
  name: string;
  meta: string;
  category: StepCategory;
  start: number;
  end: number;
  outcome: "ok" | "failed";
  fieldsFilled?: number | null;
  note?: string | null;
}

export type TraceNode = TraceGroup | TraceStep;

export interface ApplierTrace {
  title: string;
  subtitle: string;
  /** Absent when no reliable field count exists, as for a recorded run. */
  fieldsTotal?: number | null;
  root: TraceNode;
}

export type Phase = "pending" | "running" | "done" | "failed";

/** A stretch of one row's lane. A child group shows as "wait" in its parent's lane. */
export interface LaneSegment {
  type: StepCategory | "wait";
  start: number;
  end: number;
  failed: boolean;
}

export interface TraceRow {
  node: TraceNode;
  depth: number;
  ancestorIds: string[];
}

export type SessionState = "working" | "blocked" | "done";

export interface TraceSummary {
  elapsed: number;
  session: SessionState;
  fieldsFilled: number;
  waitingOnYou: number;
  toolCalls: number;
  errors: number;
  slowestStepId: string | null;
}

export function spanPhase(span: { start: number; end: number; failed: boolean }, now: number): Phase {
  if (now < span.start) return "pending";
  if (now < span.end) return "running";
  return span.failed ? "failed" : "done";
}

export function nodePhase(node: TraceNode, now: number): Phase {
  return spanPhase({ start: node.start, end: node.end, failed: node.kind === "step" && node.outcome === "failed" }, now);
}

const GAP_EPSILON = 0.05;

/**
 * Partitions a group's own lane by its children. Time between children is the
 * group's own thinking, so gaps read as "model". O(n log n) for the sort.
 */
export function laneSegments(group: TraceGroup): LaneSegment[] {
  const segments: LaneSegment[] = [];
  let cursor = group.start;
  const children = [...group.children].sort((a, b) => a.start - b.start);
  for (const child of children) {
    const type = child.kind === "group" ? "wait" : child.category;
    if (child.start > cursor + GAP_EPSILON) segments.push({ type: "model", start: cursor, end: child.start, failed: false });
    const previous = segments.at(-1);
    if (previous && previous.type === type && child.start < previous.end) {
      previous.end = Math.max(previous.end, child.end);
    } else {
      const failed = child.kind === "step" && child.outcome === "failed";
      segments.push({ type, start: Math.max(child.start, cursor), end: child.end, failed });
    }
    cursor = Math.max(cursor, child.end);
  }
  if (cursor < group.end - GAP_EPSILON) segments.push({ type: "model", start: cursor, end: group.end, failed: false });
  return segments;
}

export function rowSegments(node: TraceNode): LaneSegment[] {
  if (node.kind === "group") return laneSegments(node);
  return [{ type: node.category, start: node.start, end: node.end, failed: node.outcome === "failed" }];
}

/** Depth-first rows in display order. */
export function traceRows(root: TraceNode): TraceRow[] {
  const rows: TraceRow[] = [];
  const visit = (node: TraceNode, ancestorIds: string[]) => {
    rows.push({ node, depth: ancestorIds.length, ancestorIds });
    if (node.kind === "group") for (const child of node.children) visit(child, [...ancestorIds, node.id]);
  };
  visit(root, []);
  return rows;
}

function steps(node: TraceNode): TraceStep[] {
  return node.kind === "step" ? [node] : node.children.flatMap(steps);
}

export function traceSummary(trace: ApplierTrace, now: number): TraceSummary {
  const { root } = trace;
  const summary: TraceSummary = {
    elapsed: Math.max(0, Math.min(now, root.end) - root.start),
    session: "working",
    fieldsFilled: 0,
    waitingOnYou: 0,
    toolCalls: 0,
    errors: 0,
    slowestStepId: null,
  };
  let slowest = 0;
  for (const step of steps(root)) {
    const phase = nodePhase(step, now);
    if (phase === "pending") continue;
    if (step.category === "human") {
      summary.waitingOnYou += Math.min(now, step.end) - step.start;
      if (phase === "running") summary.session = "blocked";
      continue;
    }
    if (phase === "running") continue;
    if (phase === "done") summary.fieldsFilled += step.fieldsFilled ?? 0;
    if (phase === "failed") summary.errors += 1;
    if (step.category === "browser" || step.category === "backend") summary.toolCalls += 1;
    if (step.end - step.start > slowest) {
      slowest = step.end - step.start;
      summary.slowestStepId = step.id;
    }
  }
  if (now >= root.end) summary.session = "done";
  return summary;
}

const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];

/** The smallest round step that keeps the axis to about five ticks. */
export function tickStep(domain: number): number {
  return TICK_STEPS.find((step) => domain / step <= 5.5) ?? 600;
}

export function tickValues(domain: number): number[] {
  const step = tickStep(domain);
  const values: number[] = [];
  for (let value = 0; value <= domain; value += step) values.push(value);
  return values;
}

export function formatTick(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes}m` : `${minutes}m${rest}`;
}

/** Durations: tenths under 10s, whole seconds under a minute, then "Xm SSs". */
export function formatDuration(seconds: number): string {
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return formatClock(seconds);
}

/** Whole-second clock, stable width per unit so the KPI roll stays aligned. */
export function formatClock(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
}
