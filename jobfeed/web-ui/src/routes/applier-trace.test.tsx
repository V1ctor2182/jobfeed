import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { ApplierRun, ApplierRunTrace } from "@/api/queries";
import { SAMPLE_APPLIER_TRACE } from "@/components/trace/sample-applier-trace";
import { TraceWaterfall } from "@/components/trace/TraceWaterfall";
import ApplierTracePage from "./applier-trace";

// Rolling KPI digits are aria-hidden copies of the accessible value; skip them.
const accessibleOnly = { ignore: "script, style, [aria-hidden='true'], [aria-hidden='true'] *" };

function kpi(label: string) {
  return within(screen.getByText(label, { selector: "dt" }).parentElement!);
}

function visibleRowNames() {
  return within(screen.getByRole("list", { name: "Steps" }))
    .getAllByRole("listitem")
    .map((row) => row.textContent ?? "");
}

function renderSample(initialNow?: number, autoPlay?: boolean) {
  return render(<TraceWaterfall trace={SAMPLE_APPLIER_TRACE} initialNow={initialNow} autoPlay={autoPlay} />);
}

const RUNS: ApplierRun[] = [
  { job_id: 10, company: "Valon", title: "Software Engineer New Grad", state: "running", started_at: "2026-10-26T03:15:52+00:00", updated_at: "2026-10-26T03:20:09+00:00", is_live: true, session_count: 1 },
  { job_id: 3, company: null, title: null, state: "finished", started_at: "2026-10-26T02:31:10+00:00", updated_at: "2026-10-26T02:36:43+00:00", is_live: false, session_count: 1 },
];

const LIVE_TRACE: ApplierRunTrace = {
  job_id: 10, started_at: "2026-10-26T03:15:52+00:00", is_live: true,
  title: "Job #10", subtitle: "Software Engineer New Grad at Valon · Claude Applier · running", fieldsTotal: null,
  root: {
    kind: "group", id: "worker", name: "Worker", meta: "job #10", role: "worker", start: 0, end: 42,
    children: [
      { kind: "step", id: "launch", name: "Launch Applier", meta: "claude --bg · applier-10", category: "shell", start: 0, end: 6, outcome: "ok", fieldsFilled: null, note: null },
      {
        kind: "group", id: "applier-a", name: "Applier", meta: "Claude CLI · 9df5f433 · sonnet", role: "applier", start: 6, end: 42,
        children: [
          { kind: "step", id: "t1", name: "Fill field", meta: "form_input × 3", category: "browser", start: 8, end: 12, outcome: "ok", fieldsFilled: 3, note: null },
          { kind: "step", id: "t2", name: "Click or type", meta: "computer · left_click", category: "browser", start: 30, end: 42, outcome: "ok", fieldsFilled: null, note: null },
        ],
      },
    ],
  },
};

let responses: Record<string, unknown>;
let requested: string[];

function mountPage(path = "/applier-trace") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><ApplierTracePage /></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  requested = [];
  responses = { "/api/applier-runs": { runs: RUNS }, "/api/applier-runs/10/trace": LIVE_TRACE };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    requested.push(path);
    const body = responses[path];
    return body === undefined
      ? new Response(JSON.stringify({ error: { code: "applier_run_not_found", message: "No Applier run for job 3.", request_id: "t" } }), { status: 404 })
      : new Response(JSON.stringify(body), { status: 200 });
  }));
});
afterEach(() => vi.unstubAllGlobals());

test("lists recorded runs and opens the newest one live, with its running step and no replay controls", async () => {
  mountPage();
  expect(await screen.findByText("Valon · Software Engineer New Grad · job #10 · Live")).toBeVisible();
  await screen.findByText("Job #10");
  expect(requested).toContain("/api/applier-runs/10/trace");
  expect(screen.queryByText("Sample run")).not.toBeInTheDocument();
  const running = visibleRowNames().find((text) => text.includes("Click or type"));
  expect(running).toContain("Running");
  expect(visibleRowNames().find((text) => text.includes("Fill field"))).toContain("Done");
  expect(kpi("Session").getByText("working", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Fields filled").getByText("3", accessibleOnly)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Play" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Replay" })).not.toBeInTheDocument();
});

test("the job in the URL selects that run, and a missing trace shows an error", async () => {
  mountPage("/applier-trace?job=3");
  expect(await screen.findByText("job #3 · Finished")).toBeVisible();
  expect(await screen.findByText("No Applier run for job 3.")).toBeVisible();
  expect(requested).not.toContain("/api/applier-runs/10/trace");
});

test("with no recorded runs it says so and offers the sample as a preview", async () => {
  responses["/api/applier-runs"] = { runs: [] };
  mountPage();
  expect(await screen.findByText("No Applier has run yet")).toBeVisible();
  expect(screen.queryByText("Job #328")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Preview a sample run" }));
  expect(screen.getByText("Sample run")).toBeVisible();
  expect(screen.getByText("Job #328")).toBeVisible();
});

test("while the Applier waits on you, the trace shows started steps, the failed click and blocked KPIs", () => {
  renderSample(100, false);

  const rows = visibleRowNames();
  expect(rows.some((text) => text.includes("Ask you"))).toBe(true);
  expect(rows.some((text) => text.includes("Record answer"))).toBe(false);
  const failed = rows.filter((text) => text.includes("Pick location"));
  expect(failed).toHaveLength(2);
  expect(failed[0]).toContain("Failed");
  expect(failed[1]).toContain("Done");
  expect(rows.find((text) => text.includes("Ask you"))).toContain("Waiting on you");

  expect(kpi("Elapsed").getByText("1m 40s", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Session").getByText("blocked", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Fields filled").getByText("11/14", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Waiting on you").getByText("13s", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Tool calls").getByText("9", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Errors").getByText("1", accessibleOnly)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
});

test("collapsing the Applier hides its steps until it is expanded again", () => {
  renderSample(100, false);
  fireEvent.click(screen.getByRole("button", { name: "Collapse Applier" }));
  expect(visibleRowNames().some((text) => text.includes("Ask you"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Expand Applier" }));
  expect(visibleRowNames().some((text) => text.includes("Ask you"))).toBe(true);
});

test("a finished run reports done, marks the slowest step and offers replay", () => {
  renderSample(392, false);
  expect(kpi("Session").getByText("done", accessibleOnly)).toBeInTheDocument();
  expect(kpi("Fields filled").getByText("14/14", accessibleOnly)).toBeInTheDocument();
  const badges = within(screen.getByRole("list", { name: "Steps" })).getAllByText("Slowest", accessibleOnly);
  expect(badges).toHaveLength(1);
  expect(badges[0]!.closest("li")).toHaveTextContent("Draft free-text answers");
  expect(screen.getByRole("button", { name: "Replay" })).toBeInTheDocument();
});

test("play toggles to pause", () => {
  renderSample(100, false);
  fireEvent.click(screen.getByRole("button", { name: "Play" }));
  expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
});

test("playing advances the run at the selected speed", () => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance", "setTimeout", "clearTimeout"] });
  try {
    renderSample();
    expect(visibleRowNames()).toEqual([expect.stringContaining("Worker")]);
    act(() => { vi.advanceTimersByTime(1100); });
    const rows = visibleRowNames();
    expect(rows.some((text) => text.includes("Fill contact details"))).toBe(true);
    expect(rows.some((text) => text.includes("Upload résumé"))).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});
