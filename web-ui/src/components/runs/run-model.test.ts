import type { BatchHealth, ObservedJob } from "@/api/workspace";
import { applicationJobState, applicationStage, sourceLabel } from "./run-model";

const batch = { id: "dashboard-claude-one", freshness: "fresh", source_binding: "valid" } as BatchHealth;
const job: ObservedJob = { job_id: 3, company: "Acme", title: "Engineer", counted: true, terminal: false, user_submitted: false, owner_thread_id: "08a3d9d1307249849dc7abcca5be6293", queue_state: "Applying", turn_state: "ended",
  result_status: "ready_for_review", submitted: false, synced: false, closed: false, paused: false };
const row = (observed: ObservedJob) => ({ kind: "application" as const, id: batch.id, batch, job: observed });

it("a Claude draft that is ready for review says so and stays in Applying & other with its Submit button", () => {
  expect(applicationJobState(job)).toBe("Ready for review");
  expect(applicationStage(row(job))).toBe("other");
});

it("ready for review needs a Claude owner and no newer blocker or submission", () => {
  expect(applicationJobState({ ...job, owner_thread_id: "codex-thread" })).toBe("Applying");
  expect(applicationJobState({ ...job, blocked: "needs_input" })).toBe("needs input");
  expect(applicationJobState({ ...job, submitted: true })).toBe("Submitted");
  expect(applicationJobState({ ...job, result_status: null, turn_state: "active" })).toBe("Applying");
});

test("names the job board search source for people", () => {
  expect(sourceLabel("ats-search")).toBe("Job board search");
  expect(sourceLabel("ats-discovery")).toBe("Portfolio company discovery");
});
