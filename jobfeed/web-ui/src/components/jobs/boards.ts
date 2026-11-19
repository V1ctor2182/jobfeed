/** Shapes API payloads into board cards. Tracker shows application progress; Jobs shows the way to an application. */
import type { TrackerApplication, TrackerMessage } from "@/api/tracker";
import type { BoardCard } from "@/components/jobs/ApplicationBoard";
import { STAGES } from "@/components/jobs/ApplicationBoard";
import { formatRelativeAge } from "@/lib/dates";

function norm(value: string | null | undefined): string {
  return (value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

export function matches(search: string, ...fields: (string | null | undefined)[]): boolean {
  const needle = search.trim().toLowerCase();
  return !needle || fields.join(" ").toLowerCase().includes(needle);
}

/** The email linked to one application: by job when Jobfeed knows it, else by company. */
export function mailFor(application: TrackerApplication, messages: TrackerMessage[]): TrackerMessage[] {
  return messages.filter((message) => application.job_id != null
    ? (message.job_ids ?? []).includes(application.job_id)
    : (message.job_ids ?? []).length === 0 && norm(message.company) !== "" && norm(message.company) === norm(application.company));
}

function emails(count: number): string {
  return `${count} ${count === 1 ? "email" : "emails"}`;
}

/** One card per application, in the stage its email puts it in. */
export function progressCards(applications: TrackerApplication[]): BoardCard[] {
  return applications.map((application, index) => ({
    key: String(index),
    column: application.stage,
    company: application.company ?? "Unknown company",
    title: application.title ?? "Found in email only",
    meta: `${application.last_activity_at ? formatRelativeAge(application.last_activity_at) : "–"} · ${emails(application.email_count)}`,
    flag: application.source === "jobfeed" && !application.confirmed_by_email ? "No confirmation email" : undefined,
  }));
}

export interface QueueJob { job_id: number; company?: string | null; title?: string | null; queue_state: string; manual_priority?: boolean | null }
export interface ApplierRun { job_id: number; company: string | null; title: string | null; state: string; updated_at?: string | null }

const RUN_STATUS: Record<string, string> = { running: "Applier running", parked: "Waiting on you", finished: "Ready for review" };
export const JOB_COLUMNS = [{ id: "queued", label: "Queued" }, { id: "applying", label: "Applying" }, { id: "applied", label: "Applied" }];

export interface JobCard extends BoardCard { jobId: number; status: string; application?: TrackerApplication }

/**
 * Scanned jobs on their way to an application. Applied is every job Jobfeed records as applied. Applying is every job
 * with an Applier that is running, waiting on the user, or done and waiting for Submit. Queued is the rest of the queue.
 */
export function jobCards(queue: QueueJob[], runs: ApplierRun[], applications: TrackerApplication[]): JobCard[] {
  const applied = applications.filter((application) => application.source === "jobfeed" && application.job_id != null);
  const done = new Set(applied.map((application) => application.job_id));
  const applying = runs.filter((run) => run.state in RUN_STATUS && !done.has(run.job_id));
  const busy = new Set(applying.map((run) => run.job_id));
  const stage = (id: string) => STAGES.find((item) => item.id === id)?.label ?? id;
  return [
    ...queue.filter((job) => job.queue_state === "Queued" && !done.has(job.job_id) && !busy.has(job.job_id)).map((job) => ({
      key: `job-${job.job_id}`, jobId: job.job_id, column: "queued", status: "Queued", company: job.company ?? `Job ${job.job_id}`, title: job.title ?? "",
      meta: job.manual_priority ? "User priority" : "In the queue",
    })),
    ...applying.map((run) => ({
      key: `job-${run.job_id}`, jobId: run.job_id, column: "applying", status: RUN_STATUS[run.state] ?? run.state, company: run.company ?? `Job ${run.job_id}`, title: run.title ?? "",
      meta: `${RUN_STATUS[run.state]}${run.updated_at ? ` · ${formatRelativeAge(run.updated_at)}` : ""}`,
    })),
    ...applied.map((application) => ({
      key: `job-${application.job_id}`, jobId: application.job_id as number, column: "applied", status: "Applied", application,
      company: application.company ?? "Unknown company", title: application.title ?? "",
      meta: `${application.applied_at ? formatRelativeAge(application.applied_at) : "–"} · ${stage(application.stage)}`,
      flag: application.confirmed_by_email ? undefined : "No confirmation email",
    })),
  ];
}
