import type { ActiveRunEntry, RunSummary } from "@/api/queries";
import type { BatchHealth, ObservedJob, QueueJob } from "@/api/workspace";

export type RunSelection =
  | { kind: "pipeline"; id: string }
  | { kind: "application"; id: string; jobId?: number };
export type CurrentRun =
  | { kind: "pipeline"; id: string; run: ActiveRunEntry }
  | { kind: "application"; id: string; batch: BatchHealth; job?: ObservedJob };

export function readSelection(params: URLSearchParams): RunSelection | null {
  const kind = params.get("kind");
  const id = params.get("run");
  if (!id) return null;
  if (kind === "pipeline") return { kind, id };
  if (kind !== "application") return null;
  const job = params.get("job");
  if (job === null) return { kind, id };
  const jobId = Number(job);
  return /^\d+$/.test(job) && Number.isSafeInteger(jobId) && jobId > 0 ? { kind, id, jobId } : null;
}

export function batchIsComplete(batch: BatchHealth) {
  return batch.complete === true && batch.source_binding === "valid"
    && (batch.freshness === "fresh" || batch.freshness === "stale");
}

export function observationStatus(batch: BatchHealth) {
  if (batch.source_binding !== "valid") return `Saved observation · source ${batch.source_binding}`;
  if (batch.freshness !== "fresh") return `Saved observation · ${batch.freshness}`;
  return "Saved observation";
}

export function batchStatus(batch: BatchHealth) {
  return batch.freshness === "fresh" && batchIsComplete(batch) ? "Complete · saved observation" : observationStatus(batch);
}

export function pipelineLabel(source: string) {
  return source === "evaluate" ? "Evaluation" : `Scan · ${source === "all" ? "all sources" : source}`;
}

export function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    ashby: "Ashby",
    ats: "Company career pages",
    "ats-search": "Job board search",
    "ats-discovery": "Portfolio company discovery",
    greenhouse: "Greenhouse",
    handshake: "Handshake",
    indeed: "Indeed",
    jobright: "Jobright",
    lever: "Lever",
    linkedin: "LinkedIn",
    linkedin_guest: "LinkedIn guest",
    speedyapply: "GitHub job lists",
    speedrun: "a16z speedrun talent network",
    unknown: "Unknown source",
    url_import: "Pasted URL",
  };
  return labels[source] ?? source.replaceAll("_", " ");
}

export function pipelineProgress(run: RunSummary) {
  if (run.source === "evaluate") {
    const stage = run.progress_stage?.replaceAll("_", " ");
    return `${stage ? `${stage} · ` : ""}${run.stage_a_scored} quick / ${run.stage_b_scored} detailed`;
  }
  return `${run.jobs_discovered} discovered · ${run.jobs_inserted} new`;
}

export function applicationRuns(batches: BatchHealth[]): Extract<CurrentRun, { kind: "application" }>[] {
  return batches.flatMap<Extract<CurrentRun, { kind: "application" }>>(batch => {
    const base = { kind: "application" as const, id: batch.id, batch };
    return batch.source_binding === "valid" && batch.jobs?.length
      ? batch.jobs.map(job => ({ ...base, job })) : [base];
  });
}

export function rowSelection(item: CurrentRun): RunSelection {
  return item.kind === "application" ? { kind: item.kind, id: item.id, jobId: item.job?.job_id }
    : { kind: item.kind, id: item.id };
}

export function selectionKey(selection: RunSelection) {
  return `${selection.kind}-${selection.id}${selection.kind === "application" && selection.jobId !== undefined ? `-job-${selection.jobId}` : ""}`;
}

export function safeJobUrl(value: string | null | undefined) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

const OWNER_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;
// Claude CLI launch IDs are 32 lowercase hex characters; Codex thread IDs never take that shape.
const CLAUDE_LAUNCH_ID = /^[0-9a-f]{32}$/;

export function applierOwnerValid(owner: string | null | undefined): owner is string {
  return Boolean(owner && OWNER_ID.test(owner));
}

export function isClaudeApplier(owner: string | null | undefined) {
  return Boolean(owner && CLAUDE_LAUNCH_ID.test(owner));
}

export function agentChatUrl(owner: string | null | undefined) {
  return applierOwnerValid(owner) && !isClaudeApplier(owner) ? `codex://threads/${owner}` : null;
}

export type ApplicationRun = Extract<CurrentRun, { kind: "application" }>;
export type StoredQueueApplication = { kind: "queue"; job: QueueJob };
export type ApplicationListItem = ApplicationRun | StoredQueueApplication;
export type ApplicationStage = "applying" | "waiting" | "applied" | "other";

function normalizedState(value: string | null | undefined) {
  return value?.trim().toLowerCase().replaceAll(/\s+/g, "_") ?? "";
}

function submissionUnverified(job: ObservedJob) {
  return job.user_submitted || ["submitted", "submission_unknown", "submission_unverified"].includes(normalizedState(job.queue_state))
    || ["submission_unknown", "submission_unverified"].includes(normalizedState(job.blocked))
    || job.errors?.some(error => ["submission_unknown", "submission_unverified"].includes(error));
}

export function applicationStage(row: ApplicationListItem): ApplicationStage | null {
  if (row.kind === "queue") return "waiting";
  const job = row.job;
  if (!job) return null;
  if (job.submitted) return "applied";
  if (row.batch.source_binding !== "valid" || !["fresh", "stale"].includes(row.batch.freshness)
    || job.paused || job.blocked || job.terminal || submissionUnverified(job)) return "other";
  const state = normalizedState(job.queue_state);
  const turn = normalizedState(job.turn_state);
  if (["queued", "resume_ready"].includes(state) && !job.owner_thread_id && (!turn || turn === "not_started")) return "waiting";
  if (job.owner_thread_id && turn === "active"
    && ["dispatching", "tailoring", "applying", "in_progress", "submitting", "retrying"].includes(state)) return "applying";
  return "other";
}

export function latestApplicationRuns(batches: BatchHealth[]): ApplicationRun[] {
  const latest = new Map<number, ApplicationRun>();
  const diagnostics: ApplicationRun[] = [];
  for (const row of applicationRuns(batches)) {
    if (!row.job) { diagnostics.push(row); continue; }
    const previous = latest.get(row.job.job_id);
    const observed = Date.parse(row.batch.checked_at ?? "") || 0;
    const previousTime = Date.parse(previous?.batch.checked_at ?? "") || 0;
    if (!previous || observed > previousTime || (observed === previousTime && row.id > previous.id)) latest.set(row.job.job_id, row);
  }
  return [...latest.values(), ...diagnostics];
}

export function applicationListItems(batches: BatchHealth[], queue: QueueJob[]): ApplicationListItem[] {
  const observations = latestApplicationRuns(batches);
  const observedIds = new Set(observations.flatMap(row => row.job ? [row.job.job_id] : []));
  const queued = queue.filter(job => normalizedState(job.queue_state) === "queued" && !job.owner_thread_id && !observedIds.has(job.job_id));
  return [...observations, ...queued.map(job => ({ kind: "queue" as const, job }))];
}

export function applicationJobState(job: ObservedJob) {
  if (job.submitted) return "Submitted";
  if (submissionUnverified(job)) return "Submission unverified";
  if (job.paused) return "Paused";
  if (job.terminal) return job.queue_state ? `Terminal · ${job.queue_state.replaceAll("_", " ")}` : "Terminal";
  // A Claude Applier stops before Submit with the queue still at Applying; its own report says the draft is done.
  if (isClaudeApplier(job.owner_thread_id) && job.result_status === "ready_for_review" && !job.blocked) return "Ready for review";
  return (job.blocked || job.queue_state || job.turn_state || "State unavailable").replaceAll("_", " ");
}


export function isPartialScan(run: RunSummary): boolean {
  return run.source !== "evaluate" && run.status === "failed"
    && run.failed_stage === "source" && run.jobs_inserted > 0;
}
