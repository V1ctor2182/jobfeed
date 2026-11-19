/** A made-up Claude Applier run that follows the Phase 1 flow, shown until real runs are recorded. */
import type { ApplierTrace, StepCategory, TraceStep } from "./model";

type StepExtras = Pick<TraceStep, "fieldsFilled" | "note"> & { failed?: boolean };

const step = (category: StepCategory) =>
  (id: string, name: string, meta: string, start: number, end: number, extras: StepExtras = {}): TraceStep => {
    const { failed, ...rest } = extras;
    return { kind: "step", id, name, meta, category, start, end, outcome: failed ? "failed" : "ok", ...rest };
  };
const model = step("model");
const browser = step("browser");
const backend = step("backend");
const shell = step("shell");
const human = step("human");

export const SAMPLE_APPLIER_TRACE: ApplierTrace = {
  title: "Job #328",
  subtitle: "Platform Engineer at Northwind Labs · Greenhouse",
  fieldsTotal: 14,
  root: {
    kind: "group", id: "worker", name: "Worker", meta: "job #328", role: "worker", start: 0, end: 392,
    children: [
      backend("reserve", "Reserve job", "applications.reserve", 0.4, 1.2),
      shell("launch", "Launch Applier", "claude --bg --chrome --add-dir materials", 1.4, 6.0, { note: "Material folders are granted at launch, so uploads work without /add-dir." }),
      {
        kind: "group", id: "applier", name: "Applier", meta: "Claude CLI · a71c09f2", role: "applier", start: 6.4, end: 389.0,
        children: [
          browser("check-tab", "Check tab", "tabs_context_mcp", 7.0, 8.2, { note: "Health check before the first browser action. Owns tab 1778004417." }),
          browser("open-posting", "Open posting", "navigate · boards.greenhouse.io", 8.4, 13.6),
          browser("inspect-form", "Inspect form", "javascript_tool · inspectAutofillPage", 13.8, 17.0, { note: "14 fields found, 2 free-text questions." }),
          model("plan-answers", "Plan answers", "profile + answer bank", 17.2, 29.0),
          browser("fill-contact", "Fill contact details", "form_input × 6", 29.2, 41.0, { fieldsFilled: 6 }),
          browser("upload-resume", "Upload résumé", "file_upload · Resume-AlexDoe.pdf", 41.2, 47.4, { fieldsFilled: 1 }),
          browser("fill-eligibility", "Fill eligibility", "form_input × 3", 47.6, 54.8, { fieldsFilled: 3 }),
          browser("pick-location", "Pick location", "computer · combobox click", 55.0, 63.4, { failed: true, note: "The option list closed before the click. Retrying by typing." }),
          browser("pick-location-retry", "Pick location", "computer · type + Enter", 64.0, 70.2, { fieldsFilled: 1, note: "Retry of the failed click." }),
          model("draft-answers", "Draft free-text answers", "answer bank + job description", 70.6, 86.0),
          human("ask-salary", "Ask you", "Salary expectation for this role?", 86.4, 181.0, { note: "Session state is blocked until you answer through claude attach." }),
          backend("record-answer", "Record answer", "answers.record · rev 2", 181.4, 183.0, { note: "Saved revision 2 with its source receipt." }),
          browser("fill-answers", "Fill answers", "form_input × 3", 183.2, 194.0, { fieldsFilled: 3 }),
          browser("read-back", "Read back form", "javascript_tool · readback", 194.4, 200.2, { note: "14 of 14 fields match the saved answers." }),
          backend("save-review", "Save form review", "fields + screenshot", 200.6, 204.0),
          human("submit-approval", "Wait for Submit approval", "Dashboard confirmation", 204.4, 371.0, { note: "Only the Dashboard button counts as approval. Replies typed in the session do not." }),
          browser("check-tab-again", "Check tab", "tabs_context_mcp", 371.4, 372.4),
          browser("submit", "Submit", "computer · click Submit", 372.8, 378.6),
          browser("capture-confirmation", "Capture confirmation", "screenshot + page text", 379.0, 386.0, { note: "Page reads \"Thanks for applying\"." }),
          backend("record-submission", "Record submission", "submissions.record", 386.4, 388.6),
        ],
      },
      backend("sync-jobfeed", "Sync Jobfeed", "status → Applied", 389.2, 390.6),
      shell("stop-session", "Stop session", "claude stop a71c09f2", 390.8, 392.0, { note: "The tab stays open as evidence." }),
    ],
  },
};
