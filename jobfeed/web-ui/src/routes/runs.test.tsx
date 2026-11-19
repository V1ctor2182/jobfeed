import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import type { ApplyHealth, BatchHealth, ObservedJob, QueueJob } from "@/api/workspace";
import type { RunNewJobsResponse, RunSummary } from "@/api/queries";
import { Toaster } from "@/components/ui/toaster";
import { DensityProvider } from "@/lib/density";
import RunsPage from "@/routes/runs";
import ScannerRunsPage from "@/routes/scanner-runs";

/** Minimal EventSource stub: LiveRunRow (rendered for active runs)
 * opens an SSE connection on mount; happy-dom has no EventSource. */
class StubEventSource {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {
    // Streaming frames aren't exercised by these tests.
  }
  removeEventListener(): void {
    // Not needed for tests.
  }
  close(): void {
    // Not needed for tests.
  }
}

function runRow(id: string, over: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: id,
    started_at: "2026-06-10T08:00:00Z",
    finished_at: "2026-06-10T08:05:00Z",
    source: "ats",
    status: "finished",
    jobs_discovered: 0,
    jobs_inserted: 0,
    jobs_updated: 0,
    jobs_filtered: 0,
    jobs_ml_gated: 0,
    jobs_seniority_filtered: 0,
    jobs_scored: 0,
    stage_a_scored: 0,
    stage_b_scored: 0,
    ml_gate_processed: 0,
    stage_a_processed: 0,
    stage_b_processed: 0,
    scan_processed: 0,
    errors: 0,
    total_llm_cost_usd: 0,
    ...over,
    restart_count: over.restart_count ?? 0,
  };
}

interface ServerState {
  runs: RunSummary[];
  batches?: BatchHealth[];
  queue?: ApplyHealth["queue"];
  monitoringState?: "available" | "missing" | "unavailable";
  runtime?: ApplyHealth["runtime"];
  activeRuns: { run_id: string; source: string; started_at: string; counters: RunSummary }[];
  newJobs?: Record<string, RunNewJobsResponse | "error">;
}

interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

const calls: RecordedCall[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockApi(state: ServerState): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body === undefined ? null : JSON.parse(String(init.body));
      calls.push({ url, method, body });

      if (method === "GET" && url.startsWith("/api/application-reruns?")) return json({ csrf_token: "test-csrf", eligible: false, reason: "Recovery is unavailable for this observation.", owner_thread_id: null, request: null });
      if (method === "GET" && url === "/api/application-tabs") return json({ csrf_token: "test-csrf", bindings: [] });
      if (method === "GET" && /^\/api\/apply-health\/jobs\/\d+\/confirmation$/.test(url)) return json({ status: "missing", reason: "No saved confirmation" });
      const formRequest = /^\/api\/apply-health\/jobs\/(\d+)\/form\?/.exec(url);
      if (method === "GET" && formRequest) return json({ job_id: Number(formRequest[1]), status: "missing", reason: "No saved form", snapshots: [] });
      if (method === "GET" && url.startsWith("/api/real-jobs?")) return json({ jobs: [], total: 0, total_is_exact: true, tab_counts: {} });
      if (method === "GET" && url.startsWith("/api/application-queue")) return json({ csrf_token: "fixture-csrf", requests: [], consumer: { state: "idle", reason: null } });
      if (method === "GET" && url === "/api/apply-health") {
        return json({ monitoring: { state: state.monitoringState ?? "available" }, batches: state.batches ?? [], queue: state.queue ?? { state: "available", total: 0 }, runtime: state.runtime ?? { state: "available" } });
      }
      if (method === "GET" && url.startsWith("/api/runs?")) {
        const params = new URLSearchParams(url.slice(url.indexOf("?") + 1));
        const offset = Number(params.get("offset") ?? "0");
        const limit = Number(params.get("limit") ?? "25");
        return json({
          runs: state.runs.slice(offset, offset + limit),
          total: state.runs.length,
        });
      }
      if (method === "GET" && url === "/api/runs/active") {
        return json({ runs: state.activeRuns });
      }
      if (method === "GET" && url.startsWith("/api/runs/") && !url.endsWith("/new-job-sources")) {
        const run = state.runs.find((item) => url === `/api/runs/${item.run_id}`);
        if (run) return json({
          ...run,
          verdict_counts: run.run_id === "historical-eval"
            ? { apply: 97, consider: 40, skip: 12 }
            : run.verdict_counts,
        });
      }
      if (method === "GET" && url === "/api/runs/r2/new-job-sources") {
        return json({
          run_id: "r2",
          source_counts: {
            ats: 2,
            indeed: 1,
            linkedin_guest: 1,
          },
          total: 4,
        });
      }
      const newJobsRun = /^\/api\/runs\/([^/]+)\/new-jobs$/.exec(url)?.[1];
      if (method === "GET" && newJobsRun !== undefined) {
        const newJobs = state.newJobs?.[newJobsRun] ?? { run_id: newJobsRun, jobs: [], total: 0, is_truncated: false };
        return newJobs === "error"
          ? json({ error: { code: "internal", message: "boom", request_id: "req-1" } }, 500)
          : json(newJobs);
      }
      if (method === "POST" && url === "/api/runs/scan") {
        const { source } = body as { source: string };
        if (source === "conflict") {
          return json(
            { error: { code: "scan_already_running", message: "Scan already running", request_id: "req-1" } },
            409,
          );
        }
        return json({ run_id: "new-scan-1", status: "running" });
      }
      if (method === "POST" && url === "/api/runs/evaluate") {
        return json({ run_id: "new-eval-1", status: "running" });
      }
      if (method === "POST" && url.endsWith("/stop")) {
        return json({ run_id: url.split("/")[3], status: "failed" });
      }
      if (method === "POST" && url.endsWith("/retry")) {
        return json({ run_id: "retry-1", status: "running" });
      }
      throw new Error(`unexpected fetch in test: ${method} ${url}`);
    }),
  );
}

function BackButton() {
  const navigate = useNavigate();
  const location = useLocation();
  return <><button onClick={() => navigate(-1)}>Browser back</button><output aria-label="Current URL">{location.search}</output><output aria-label="Current path">{location.pathname}</output></>;
}

function renderRuns(openHistory = true, entry = "/scanner-runs") {
  if (entry === "/runs") entry = "/runs?tab=all";
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <MemoryRouter initialEntries={[entry]}><BackButton />
    <QueryClientProvider client={queryClient}>
      <DensityProvider>
        <Routes><Route path="/runs" element={<RunsPage />} /><Route path="/scanner-runs" element={<ScannerRunsPage />} /></Routes>
        <Toaster />
      </DensityProvider>
    </QueryClientProvider></MemoryRouter>,
  );
  if (openHistory && entry.startsWith("/scanner-runs")) fireEvent.click(screen.getByRole("button", { name: "Scanner history" }));
  return { ...result, queryClient };
}

let state: ServerState;

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal("EventSource", StubEventSource);
  state = {
    runs: [
      runRow("r2", {
        started_at: "2026-06-10T09:30:00Z",
        jobs_discovered: 12,
        jobs_inserted: 4,
        jobs_updated: 3,
        jobs_ml_gated: 2,
        stage_a_scored: 8,
        stage_b_scored: 1,
        total_llm_cost_usd: 0.42,
      }),
      runRow("r1", { started_at: "2026-06-09T07:00:00Z", errors: 2, finished_at: null }),
    ],
    activeRuns: [],
  };
  mockApi(state);
});

afterEach(() => {
  window.localStorage.removeItem("jobfeed:density");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("renders the Cloudscape run operations workspace and history table", async () => {
  renderRuns();

  expect(await screen.findByTestId("cloudscape-scanner-runs")).toBeVisible();
  expect(screen.getByRole("heading", { name: "Scanner activity", level: 2 })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Scanner history", level: 2 })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "Started" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "Run type" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "Activity" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "Cost" })).toBeVisible();
});

test("keeps full filter counters in the selected details", async () => {
  state.runs[0] = runRow("r2", { jobs_filtered: 4, jobs_ml_gated: 2, jobs_seniority_filtered: 3 });
  renderRuns();
  fireEvent.click(within(await screen.findByTestId("run-row-r2")).getByRole("button"));
  const details = screen.getByRole("region", { name: "Selected run details" });
  expect(details).toHaveTextContent("excluded by job rules4");
  expect(details).toHaveTextContent("excluded by SDE role filter2");
  expect(details).toHaveTextContent("excluded by seniority filter3");
});

test("shows a completed evaluation's own recommendation counts when expanded", async () => {
  state.runs = [runRow("eval-finished", {
    source: "evaluate",
    stage_b_scored: 6,
    verdict_counts: { apply: 3, consider: 2, skip: 1 },
  })];
  renderRuns();

  const row = await screen.findByTestId("run-row-eval-finished");
  expect(screen.getByTestId("run-activity-eval-finished")).not.toHaveTextContent("3 Apply");
  fireEvent.click(within(row).getByRole("button", { name: /Run started/ }));
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("Apply")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("Consider")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("Ignore")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("3")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("2")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("1")).toBeVisible();
});

test("loads an old evaluation's verified breakdown only when its row opens", async () => {
  state.runs = [runRow("historical-eval", {
    source: "evaluate", stage_b_scored: 149, verdict_counts: null,
  })];
  renderRuns();
  const row = await screen.findByTestId("run-row-historical-eval");
  expect(calls.some((call) => call.url === "/api/runs/historical-eval")).toBe(false);
  fireEvent.click(within(row).getByRole("button", { name: /Run started/ }));
  expect(await within(screen.getByRole("region", { name: "Selected run details" })).findByText("97")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("40")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("12")).toBeVisible();
  expect(calls.some((call) => call.url === "/api/runs/historical-eval")).toBe(true);
});

test("explains how the latest scan becomes the evaluation candidate set", async () => {
  const activeEvaluation = runRow("eval-active", {
    started_at: "2026-06-10T10:00:00Z",
    finished_at: null,
    source: "evaluate",
    status: "running",
    evaluation_scope: "latest_scan",
    evaluation_input_total: 2141,
    progress_stage: "stage_a",
    jobs_filtered: 97,
    jobs_ml_gated: 320,
    jobs_seniority_filtered: 481,
    ml_gate_total: 1290,
    ml_gate_processed: 1290,
    stage_a_total: 489,
    stage_a_processed: 20,
  });
  state.runs = [
    activeEvaluation,
    runRow("scan-before-eval", {
      started_at: "2026-06-10T09:00:00Z",
      source: "all",
      status: "succeeded",
      jobs_inserted: 2141,
    }),
  ];
  state.activeRuns = [{
    run_id: activeEvaluation.run_id,
    source: activeEvaluation.source,
    started_at: activeEvaluation.started_at,
    counters: activeEvaluation,
  }];

  renderRuns();

  fireEvent.click(await screen.findByRole("button", { name: "Open Evaluation details" }));
  const active = await screen.findByTestId("live-run-eval-active");
  expect(active).toHaveTextContent("Latest scan: 2141 listings → 1290 candidates");
  expect(active).toHaveTextContent("754 not eligible or duplicate");
  expect(active).toHaveTextContent("97 excluded by job rules");
  expect(within(active).getByRole("progressbar", {
    name: "Candidate preparation: Latest scan: 2141 listings → 1290 candidates",
  })).toBeVisible();
});

test("labels a backlog evaluation from its own scope, not the preceding scan", async () => {
  const activeEvaluation = runRow("eval-backlog", {
    started_at: "2026-06-10T10:00:00Z",
    finished_at: null,
    source: "evaluate",
    status: "running",
    evaluation_scope: "backlog",
    evaluation_input_total: 131699,
    progress_stage: "stage_a",
    ml_gate_total: 5867,
    ml_gate_processed: 5867,
    jobs_filtered: 1133,
    jobs_ml_gated: 442,
    jobs_seniority_filtered: 5237,
    stage_a_total: 188,
    stage_a_processed: 18,
  });
  state.runs = [
    activeEvaluation,
    runRow("scan-before-backlog", {
      started_at: "2026-06-10T09:00:00Z",
      source: "all",
      status: "succeeded",
      jobs_inserted: 2411,
    }),
  ];
  state.activeRuns = [{
    run_id: activeEvaluation.run_id,
    source: activeEvaluation.source,
    started_at: activeEvaluation.started_at,
    counters: activeEvaluation,
  }];

  renderRuns();

  fireEvent.click(await screen.findByRole("button", { name: "Open Evaluation details" }));
  const active = await screen.findByTestId("live-run-eval-backlog");
  expect(active).toHaveTextContent("Historical backlog → 7000 candidates");
  expect(active).not.toHaveTextContent("2411 new listings");
  expect(active).not.toHaveTextContent("already evaluated or duplicate");
});

test("run history table follows the selected row density", async () => {
  window.localStorage.setItem("jobfeed:density", "comfortable");
  renderRuns();

  expect(await screen.findByTestId("run-history-density")).toHaveAttribute(
    "data-density",
    "comfortable",
  );
});

test("rows mirror the server's newest-first order with mono timestamps", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  const rows = screen.getAllByTestId(/^run-row-/);
  expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
    "run-row-r2",
    "run-row-r1",
  ]);
  // Local-formatted wall clock in the mono column (TZ-agnostic shape).
  expect(
    within(rows[0]!).getByRole("button", { name: /Run started \d{4}-\d{2}-\d{2} \d{2}:\d{2}/ }),
  ).toBeInTheDocument();
  // The read-only hint has been removed.
  expect(screen.queryByText(/triggering scans from this page arrives with Phase 9/)).toBeNull();
});

test("history shows only a compact activity summary", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");
  expect(screen.getByTestId("run-activity-r2")).toHaveTextContent("12 discovered · 4 new");
  expect(screen.queryByText("2 excluded by SDE role filter")).toBeNull();
  expect(screen.getByText("$0.42")).toBeVisible();
});

test("failed runs use an error indicator instead of a success check", async () => {
  state.runs = [runRow("failed-run", { status: "failed" })];
  renderRuns();

  const failed = await screen.findByText("Failed");
  expect(failed.closest('[class*="status-error"]')).not.toBeNull();
  expect(screen.getByText("0 discovered · 0 new")).toBeVisible();
});

test("interrupted runs show durable progress and their automatic replacement", async () => {
  state.runs = [runRow("old-run", {
    status: "failed",
    jobs_discovered: 1666,
    jobs_updated: 1653,
    failure_code: "interrupted",
    failure_message: "Run interrupted after its worker stopped responding",
    failed_stage: "scan",
    failed_source: "linkedin_guest",
    last_progress_at: "2026-06-10T08:04:00Z",
    restart_count: 0,
    restarted_by_run_id: "replacement-run",
  })];
  renderRuns();

  await screen.findByTestId("run-row-old-run");
  expect(
    (await screen.findByText("Failed")).closest('[class*="status-error"]'),
  ).not.toBeNull();
  expect(screen.getByText(/1666 discovered/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Run started/ }));
  expect(screen.getByText("replacement-run")).toBeVisible();
});

test("running rows can be stopped and failed rows can be retried", async () => {
  state.runs = [
    runRow("stale-run", { status: "running", finished_at: null }),
    runRow("failed-run", { status: "failed" }),
    runRow("succeeded-run"),
  ];
  renderRuns();

  fireEvent.click(within(await screen.findByTestId("run-row-stale-run")).getByRole("button"));
  fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
  fireEvent.click(within(screen.getByTestId("run-row-failed-run")).getByRole("button"));
  const retryButtons = screen.getAllByRole("button", { name: "Retry" });
  expect(retryButtons).toHaveLength(1);
  fireEvent.click(retryButtons[0]!);

  await waitFor(() => {
    expect(calls.some((call) => call.method === "POST" && call.url === "/api/runs/stale-run/stop")).toBe(true);
    expect(calls.some((call) => call.method === "POST" && call.url === "/api/runs/failed-run/retry")).toBe(true);
  });
});

test("completed evaluations with errors can continue their failed work", async () => {
  state.runs = [
    runRow("completed-with-errors", {
      source: "evaluate",
      status: "succeeded",
      errors: 2,
    }),
    runRow("clean-evaluation", {
      source: "evaluate",
      status: "succeeded",
      errors: 0,
    }),
  ];
  renderRuns();

  fireEvent.click(within(await screen.findByTestId("run-row-completed-with-errors")).getByRole("button"));
  const retry = await screen.findByRole("button", { name: "Retry" });
  expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  fireEvent.click(retry);

  await waitFor(() => {
    expect(calls.some((call) =>
      call.method === "POST"
      && call.url === "/api/runs/completed-with-errors/retry"
    )).toBe(true);
  });
});

test("expanding a row reveals the full counter grid, run id, and finished time", async () => {
  renderRuns();
  const row = await screen.findByTestId("run-row-r2");
  const toggle = within(row).getByRole("button", { name: /Run started/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");

  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  // Zero counters appear in the expanded grid even though their chips don't.
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("updated")).toBeInTheDocument();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("evaluated")).toBeInTheDocument();
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("finished")).toBeInTheDocument();
  expect(within(screen.getByRole("dialog", { name: / details/ })).getByText("r2")).toBeInTheDocument();
  expect(await within(screen.getByRole("region", { name: "Selected run details" })).findByText("New jobs by source")).toBeVisible();
  expect(await within(screen.getByRole("region", { name: "Selected run details" })).findByTestId("run-source-ats")).toHaveTextContent("Company career pages 2");
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByTestId("run-source-indeed")).toHaveTextContent("Indeed 1");
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByTestId("run-source-linkedin_guest")).toHaveTextContent("LinkedIn guest 1");
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByText("4 total new listings")).toBeVisible();
  expect(calls.some((call) => call.url === "/api/runs/r2/new-job-sources")).toBe(true);

  fireEvent.click(toggle);
  expect(within(row).queryByText("updated")).toBeNull();
});

test("clicking anywhere on a history row opens and closes its details", async () => {
  renderRuns();
  const row = (await screen.findByTestId("run-row-r2")).closest("tr")!;
  const toggle = within(row).getByRole("button", { name: /Run started/ });

  fireEvent.click(within(row).getAllByRole("cell")[1]!);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("region", { name: "Selected run details" })).toBeVisible();

  fireEvent.click(within(row).getAllByRole("cell")[1]!);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("region", { name: "Selected run details" })).toBeNull();
});

function newJob(id: string, over: Partial<RunNewJobsResponse["jobs"][number]> = {}): RunNewJobsResponse["jobs"][number] {
  return {
    job_id: id,
    title: `Engineer ${id}`,
    company: `Company ${id}`,
    location: "Remote",
    source: "indeed",
    platform: "indeed",
    url: `https://www.indeed.com/viewjob?jk=${id}`,
    first_seen_at: "2026-06-10T09:31:00Z",
    ...over,
  };
}

function openScanDetails() {
  return screen.findByTestId("run-row-r2").then((row) => {
    fireEvent.click(within(row).getByRole("button", { name: /Run started/ }));
    return screen.getByRole("region", { name: "Selected run details" });
  });
}

test("a scan's details list the postings it found with links to each job", async () => {
  state.newJobs = {
    r2: {
      run_id: "r2",
      jobs: [
        newJob("41", { title: "Backend Engineer", company: "Acme", source: "ats", platform: "lever", url: "https://jobs.lever.co/acme/41" }),
        newJob("42", { title: "Platform Engineer", company: "Globex", location: "Austin, TX" }),
      ],
      total: 3,
      is_truncated: true,
    },
  };
  renderRuns();
  const details = await openScanDetails();

  const table = await within(details).findByRole("table", { name: "New postings from this scan" });
  const acme = within(table).getByRole("row", { name: /Backend Engineer/ });
  expect(acme).toHaveTextContent("Acme");
  expect(acme).toHaveTextContent("Company career pages");
  expect(within(acme).getByRole("link", { name: /Open posting/ })).toHaveAttribute("href", "https://jobs.lever.co/acme/41");
  const globex = within(table).getByRole("row", { name: /Platform Engineer/ });
  expect(globex).toHaveTextContent("Austin, TX");
  expect(globex).toHaveTextContent("Indeed");
  expect(within(details).getByText("Showing the first 2 of 3 new postings.")).toBeVisible();

  fireEvent.click(within(acme).getByRole("button", { name: "Open Acme Backend Engineer" }));
  expect(await screen.findByRole("dialog", { name: "Job scores and evidence" })).toBeVisible();
  expect(calls.some((call) => call.url === "/api/runs/r2/new-jobs")).toBe(true);
});

test("a scan's postings list explains when it is empty or unavailable", async () => {
  state.newJobs = { r2: { run_id: "r2", jobs: [], total: 0, is_truncated: false } };
  const { unmount } = renderRuns();
  expect(await within(await openScanDetails()).findByText("No new postings recorded")).toBeVisible();
  unmount();

  state.newJobs = { r2: "error" };
  renderRuns();
  const details = await openScanDetails();
  expect(await within(details).findByText("New postings unavailable")).toBeVisible();
  expect(within(details).queryByRole("table", { name: "New postings from this scan" })).toBeNull();
});

test("pagination drives offset and the mono range label", async () => {
  state.runs = Array.from({ length: 30 }, (_, i) => runRow(`p${30 - i}`));
  renderRuns();
  await screen.findByText("1–25 of 30");
  expect(screen.getByRole("button", { name: "Previous page" })).toHaveAttribute("aria-disabled", "true");

  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  await screen.findByText("26–30 of 30");
  expect(calls.some((c) => c.url.includes("offset=25"))).toBe(true);
  expect(screen.getByRole("button", { name: "Next page" })).toHaveAttribute("aria-disabled", "true");

  fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
  await screen.findByText("1–25 of 30");
});

test("empty history points to the visible scan action", async () => {
  state.runs = [];
  renderRuns();

  expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  expect(screen.getByText("Use Start scan above to collect jobs.")).toBeInTheDocument();
  expect(screen.getByText("0 runs")).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Trigger buttons
// ---------------------------------------------------------------------------

test("trigger scan and evaluate buttons render in the header", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  // Both trigger buttons are present.
  expect(screen.getByRole("button", { name: "Start scan" })).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Start evaluation" }).length).toBeGreaterThanOrEqual(1);

  // The read-only hint is gone.
  expect(screen.queryByText(/read-only/i)).toBeNull();
  expect(screen.queryByText(/Phase 9/)).toBeNull();
});

test.each([
  ["All enabled sources", "all"],
  ["Company career pages", "ats"],
  ["Job board search", "ats-search"],
  ["Portfolio company discovery", "ats-discovery"],
  ["GitHub job lists", "speedyapply"],
  ["a16z speedrun talent network", "speedrun"],
  ["Indeed", "indeed"],
  ["LinkedIn Guest", "linkedin-guest"],
  ["LinkedIn · browser profile", "linkedin"],
  ["Jobright recommendations", "jobright"],
  ["LinkedIn · signed in", "linkedin-extension"],
  ["Handshake · signed in", "handshake"],
])("starts the selected %s source", async (label, source) => {
  renderRuns();
  await screen.findByTestId("run-row-r2");
  fireEvent.click(screen.getByRole("button", { name: "Start scan" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: label }));
  await waitFor(() => {
    expect(calls.find((call) => call.method === "POST" && call.url === "/api/runs/scan")?.body)
      .toEqual({ source });
  });
  expect(await screen.findByText(`Scan started (${label})`)).toBeInTheDocument();
});

test("trigger evaluate dialog opens and submits with defaults", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  fireEvent.click(screen.getAllByRole("button", { name: "Start evaluation" })[0]!);
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("heading", { name: "Start evaluation" })).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /Quick score and detailed review/ })).toBeInTheDocument();
  expect(screen.getByLabelText("Maximum jobs")).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Start evaluation" })).toBeInTheDocument();

  fireEvent.click(within(dialog).getByRole("button", { name: "Start evaluation" }));

  const postCall = await waitFor(() => {
    const call = calls.find((c) => c.method === "POST" && c.url === "/api/runs/evaluate");
    expect(call).toBeDefined();
    return call!;
  });
  expect(postCall.body).toEqual({ stage: "both", scope: "latest_scan" });
  expect(await screen.findByText(/Evaluation started/)).toBeInTheDocument();
});

test("trigger evaluate submits a non-default stage", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  fireEvent.click(screen.getAllByRole("button", { name: "Start evaluation" })[0]!);
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(screen.getByRole("radio", { name: /Detailed review only/ }));
  fireEvent.change(screen.getByLabelText("Maximum jobs"), { target: { value: "1" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Start evaluation" }));

  const postCall = await waitFor(() => {
    const call = calls.find((c) => c.method === "POST" && c.url === "/api/runs/evaluate");
    expect(call).toBeDefined();
    return call!;
  });
  expect(postCall.body).toEqual({
    stage: "b",
    limit: 1,
    scope: "latest_scan",
  });
});

test("evaluate 409 shows conflict toast", async () => {
  // Override mock to return 409 for evaluate.
  const baseFetch = globalThis.fetch;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "POST" && url === "/api/runs/evaluate") {
        return json(
          { error: { code: "evaluate_already_running", message: "Evaluate already running", request_id: "req-2" } },
          409,
        );
      }
      return baseFetch(input, init);
    }),
  );

  renderRuns();
  await screen.findByTestId("run-row-r2");

  fireEvent.click(screen.getAllByRole("button", { name: "Start evaluation" })[0]!);
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Start evaluation" }));

  expect(await screen.findByText("Evaluation already running")).toBeInTheDocument();
});

test("incomplete canonical evaluation shows its readiness error", async () => {
  const baseFetch = globalThis.fetch;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST" && String(input) === "/api/runs/evaluate") {
        return json({ error: {
          code: "canonical_evaluation_not_ready",
          message: "canonical evaluation activation has evaluation backfill",
          request_id: "req-ready",
        } }, 409);
      }
      return baseFetch(input, init);
    }),
  );

  renderRuns();
  await screen.findByTestId("run-row-r2");
  fireEvent.click(screen.getAllByRole("button", { name: "Start evaluation" })[0]!);
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Start evaluation" }));

  expect(await screen.findByText("Evaluation could not start")).toBeInTheDocument();
  expect(screen.getByText(/evaluation backfill/)).toBeInTheDocument();
});

test("active runs query fetches GET /api/runs/active", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  // The active runs endpoint was called.
  expect(calls.some((c) => c.url === "/api/runs/active" && c.method === "GET")).toBe(true);
});

test("invalid limit disables submit, shows inline error, and blocks the POST", async () => {
  renderRuns();
  await screen.findByTestId("run-row-r2");

  fireEvent.click(screen.getAllByRole("button", { name: "Start evaluation" })[0]!);
  const dialog = await screen.findByRole("dialog");

  fireEvent.change(screen.getByLabelText("Maximum jobs"), { target: { value: "0" } });
  expect(screen.getByText(/at least 1/)).toBeInTheDocument();
  expect(screen.getByLabelText("Maximum jobs")).toHaveAttribute("aria-invalid", "true");

  const submit = within(dialog).getByRole("button", { name: "Start evaluation" });
  expect(submit).toBeDisabled();
  fireEvent.click(submit);
  expect(calls.some((c) => c.method === "POST" && c.url === "/api/runs/evaluate")).toBe(false);

  // Clearing the field recovers.
  fireEvent.change(screen.getByLabelText("Maximum jobs"), { target: { value: "5" } });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(within(dialog).getByRole("button", { name: "Start evaluation" })).toBeEnabled();
});

test("a running run renders only as a live row, not in history too", async () => {
  // r2 is running: /api/runs/active serves it AND it still appears in
  // the /api/runs history payload for its entire duration.
  state.activeRuns = [
    {
      run_id: "r2",
      source: "ats",
      started_at: "2026-06-10T09:30:00Z",
      counters: runRow("r2", { status: "running", finished_at: null }),
    },
  ];
  renderRuns();

  await screen.findByTestId("current-run-pipeline-r2");
  expect(screen.queryByTestId("live-run-r2")).toBeNull();
  await screen.findByTestId("run-row-r1");
  // Deduped: the running run stays out of the history list.
  expect(screen.queryByTestId("run-row-r2")).toBeNull();
});

function batchRow(id: string, overrides: Partial<BatchHealth> = {}): BatchHealth {
  return { id, freshness: "fresh", source_binding: "valid", complete: false, checked_at: "2026-06-10T10:00:00Z",
    jobs: [], jobs_truncated: false, target: 5,
    counts: { submitted: 2, synced: 1, closed: 1, occupied: 1, paused: 0, reserved: 0 },
    blockers: { awaiting_user: 1, pending_controller: 0, waiting_result: 0 },
    queue_health: { state: "no_active_issue_recorded" }, ...overrides };
}

function setActive(id = "live") {
  const run = runRow(id, { status: "running", finished_at: null });
  state.activeRuns = [{ run_id: id, source: run.source, started_at: run.started_at, counters: run }];
  state.runs = [run];
}

test("starts compact, then opens and closes a selected run with focus restoration", async () => {
  setActive();
  renderRuns(false);
  const trigger = await screen.findByRole("button", { name: "Open Scan · ats details" });
  expect(screen.queryByTestId("live-run-live")).toBeNull();
  expect(screen.getByRole("button", { name: "Scanner history" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(trigger);
  const details = screen.getByRole("region", { name: "Selected run details" });
  expect(screen.getByRole("dialog", { name: / details/ })).toContainElement(details);
  expect(within(details).getByTestId("live-run-live")).toBeVisible();
  expect(trigger.closest("tr")).not.toContainElement(details);
  fireEvent.click(screen.getByRole("button", { name: "Close run details" }));
  expect(screen.queryByRole("region", { name: "Selected run details" })).toBeNull();
  await waitFor(() => expect(trigger).toHaveFocus());
});

test("browser Back restores the unselected list", async () => {
  setActive();
  renderRuns(false);
  fireEvent.click(await screen.findByRole("button", { name: "Open Scan · ats details" }));
  expect(screen.getByRole("region", { name: "Selected run details" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
  expect(screen.queryByRole("region", { name: "Selected run details" })).toBeNull();
});

test("loads a selected pipeline run directly from the URL", async () => {
  renderRuns(false, "/runs?kind=pipeline&run=r2");
  await screen.findByRole("region", { name: "Selected run details" });
  expect(await within(screen.getByRole("dialog", { name: / details/ })).findByText("r2")).toBeVisible();
  expect(screen.queryByTestId("live-run-r2")).toBeNull();
});

test("an unavailable selected id has a clear recovery control", async () => {
  renderRuns(false, "/runs?kind=application&run=missing");
  expect(await screen.findByText(/This application run is unavailable/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Close run details" }));
  expect(screen.queryByText(/This application run is unavailable/)).toBeNull();
});

test("application rows show saved evidence and open one batch without a picker", async () => {
  state.runs = [];
  state.batches = [batchRow("batch-a"), batchRow("batch-b")];
  renderRuns(false, "/runs");
  expect(await screen.findAllByText(/1 needs input/)).toHaveLength(2);
  fireEvent.click(within(screen.getByTestId("current-run-application-batch-b")).getByRole("button", { name: "Open Application batch details" }));
  const details = screen.getByRole("region", { name: "Selected run details" });
  expect(details).toHaveTextContent("batch-b");
  expect(details).not.toHaveTextContent("batch-a");
  expect(within(details).getByRole("heading", { name: /Observed jobs/ })).toBeVisible();
  expect(within(details).queryByRole("combobox")).toBeNull();
  expect(screen.queryByText("Running")).toBeNull();
});

test("stale, unknown, and invalid-binding batches are never inferred complete from empty jobs", async () => {
  state.batches = [batchRow("stale", { freshness: "stale", complete: null }),
    batchRow("unknown", { complete: null }), batchRow("invalid", { source_binding: "invalid", complete: true }),
    batchRow("finished", { complete: true })];
  renderRuns(false, "/runs");
  await screen.findByTestId("current-run-application-stale");
  expect(screen.getByTestId("current-run-application-unknown")).toBeVisible();
  expect(screen.getByTestId("current-run-application-invalid")).toBeVisible();
  expect(screen.getByText("Saved observation · stale")).toBeVisible();
  expect(screen.getByText("Saved observation · source invalid")).toBeVisible();
  const completed = screen.getByRole("table", { name: "Applications" });
  expect(within(completed).getByTestId("current-run-application-finished")).toBeVisible();
  expect(within(completed).getByTestId("current-run-application-invalid")).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applications (0)" })).toBeVisible();
});

test("pipeline records remain visible while application monitoring is loading", async () => {
  setActive();
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/apply-health" ? new Promise(() => {}) : fetch(input, init));
  renderRuns(false);
  expect(await screen.findByRole("button", { name: "Open Scan · ats details" })).toBeVisible();
  expect(screen.queryByRole("status", { name: "Loading application health" })).toBeNull();
  expect(calls.some(call => call.url === "/api/apply-health")).toBe(false);
});

test("failed reads are distinguished from an empty current list", async () => {
  vi.stubGlobal("fetch", async () => json({ error: { code: "unavailable", message: "Unavailable", request_id: "test" } }, 503));
  renderRuns(false);
  expect(await screen.findByText("Current pipeline runs unavailable")).toBeVisible();
  expect(screen.queryByText("Apply health unavailable")).toBeNull();
  expect(screen.queryByText("No current runs recorded.")).toBeNull();
});

test("empty scanner data is independent of application monitoring", async () => {
  state.runs = [];
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/apply-health" ? new Promise(() => {}) : fetch(input, init));
  renderRuns(false);
  await waitFor(() => expect(calls.some(call => call.url === "/api/runs/active")).toBe(true));
  expect(screen.queryByText("No current runs recorded.")).toBeNull();
  expect(await screen.findByText("No active scans or evaluations.")).toBeVisible();
  expect(calls.some(call => call.url === "/api/apply-health")).toBe(false);
});

test("retry refreshes an off-page detail and removes its obsolete retry action", async () => {
  state.runs = [];
  let detail = runRow("off-page", { status: "failed" });
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/runs/off-page") return json(detail);
    if (String(input) === "/api/runs/off-page/retry") {
      detail = { ...detail, restarted_by_run_id: "replacement" };
      return json({ run_id: "replacement", status: "running" });
    }
    return fetch(input, init);
  });
  renderRuns(false, "/runs?kind=pipeline&run=off-page");
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  await screen.findByText("replacement");
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

test("manual refresh updates an off-page selected terminal run", async () => {
  state.runs = [];
  let detail = runRow("off-page", { status: "failed", failure_message: "Earlier failure" });
  let fail = false;
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/runs/off-page" ? fail ? json({ error: { code: "unavailable", message: "Unavailable", request_id: "test" } }, 503) : json(detail) : fetch(input, init));
  renderRuns(false, "/runs?kind=pipeline&run=off-page");
  await screen.findByText("Failed: Earlier failure");
  detail = { ...detail, failure_message: "Updated failure" };
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("Failed: Updated failure")).toBeVisible();
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("Run details refresh failed")).toBeVisible();
  expect(screen.getByText("Failed: Updated failure")).toBeVisible();
});

test("a polled completion refreshes history without opening the run", async () => {
  setActive();
  const { queryClient } = renderRuns();
  await screen.findByTestId("current-run-pipeline-live");
  expect(screen.queryByTestId("live-run-live")).toBeNull();
  state.activeRuns = [];
  state.runs = [runRow("live", { status: "succeeded" })];
  await act(async () => { await queryClient.refetchQueries({ queryKey: ["runs", "active"] }); });
  expect(await screen.findByTestId("run-row-live")).toBeVisible();
  expect(await screen.findByText("Succeeded")).toBeVisible();
  expect(screen.queryByRole("region", { name: "Selected run details" })).toBeNull();
});

function observedJob(id: number, overrides: Partial<ObservedJob> = {}): ObservedJob {
  return { job_id: id, title: `Engineer ${id}`, company: "Example Co", counted: true, closed: false,
    paused: false, submitted: false, synced: false, terminal: false, user_submitted: false,
    queue_state: "in_progress", turn_state: "active", next_step: "Review application fields", owner_thread_id: `owner-${id}`,
    job_url: `https://example.com/jobs/${id}`, ...overrides };
}

test("individual job rows open exact details and preserve URL, Back, and close focus", async () => {
  state.batches = [batchRow("batch-jobs", { jobs: [observedJob(101), observedJob(202)] })];
  renderRuns(false, "/runs");
  const first = await screen.findByRole("button", { name: "Open Engineer 101 details" });
  const second = screen.getByRole("button", { name: "Open Engineer 202 details" });
  expect(screen.getByTestId("current-run-application-batch-jobs-job-101")).toHaveTextContent("Example Co");
  fireEvent.click(first);
  let details = screen.getByRole("region", { name: "Selected run details" });
  expect(within(details).getByRole("heading", { name: "Engineer 101" })).toBeVisible();
  expect(within(details).getByText("owner-101")).toBeVisible();
  expect(screen.getByLabelText("Current URL")).toHaveTextContent("kind=application&run=batch-jobs&job=101");
  expect(within(details).getByRole("button", { name: "Batch diagnostics" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(second);
  details = screen.getByRole("region", { name: "Selected run details" });
  expect(within(details).getByRole("heading", { name: "Engineer 202" })).toBeVisible();
  expect(details).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByRole("heading", { name: "Engineer 101" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Close run details" }));
  await waitFor(() => expect(first).toHaveFocus());
  expect(screen.getByLabelText("Current URL")).toHaveTextContent("?tab=all");
});

test("safe job URLs and recorded agent chats have distinct links", async () => {
  state.batches = [batchRow("links", { jobs: [observedJob(101)] })];
  renderRuns(false, "/runs");
  const row = await screen.findByTestId("current-run-application-links-job-101");
  expect(within(row).getByRole("link", { name: /Open job posting/ })).toHaveAttribute("href", "https://example.com/jobs/101");
  expect(within(row).getByRole("button", { name: "Open application form" })).not.toHaveAttribute("href");
  expect(within(row).getByRole("link", { name: "Open applier chat" })).toHaveAttribute("href", "codex://threads/owner-101");
  fireEvent.click(within(row).getByRole("button", { name: "Open Engineer 101 details" }));
  expect(screen.getByText(/Open application form brings the applier’s existing Chrome tab forward/)).toBeVisible();
});

test("unsafe or missing job URLs and owners never become links", async () => {
  state.batches = [batchRow("unsafe", { jobs: [
    observedJob(1, { job_url: "javascript:alert(1)", owner_thread_id: "../../bad" }),
    observedJob(2, { job_url: "https://name:secret@example.com/job", owner_thread_id: "bad/thread" }),
    observedJob(3, { job_url: null, owner_thread_id: null }),
  ] })];
  renderRuns(false, "/runs");
  for (const id of [1, 2, 3]) {
    const row = await screen.findByTestId(`current-run-application-unsafe-job-${id}`);
    expect(within(row).getByText("Job URL unavailable")).toBeVisible();
    expect(within(row).getByText("Applier chat unavailable")).toBeVisible();
    expect(within(row).queryByRole("link")).toBeNull();
  }
});

test("newest bound observation wins current rows while deep links keep exact provenance", async () => {
  state.batches = [batchRow("older", { checked_at: "2026-06-10T09:00:00Z", jobs: [observedJob(101, { title: "Old observation" })] }),
    batchRow("newer", { checked_at: "2026-06-10T10:00:00Z", jobs: [observedJob(101, { title: "Latest observation" })] })];
  renderRuns(false, "/runs?kind=application&run=older&job=101");
  expect(await screen.findByRole("button", { name: "Open Latest observation details" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Old observation details" })).toBeNull();
  const details = screen.getByRole("region", { name: "Selected run details" });
  expect(within(details).getByRole("heading", { name: "Old observation" })).toBeVisible();
});

test("stage filters keep applied jobs visible and separate paused and terminal states", async () => {
  state.batches = [batchRow("mixed", { complete: false, jobs: [
    observedJob(1, { terminal: true }), observedJob(2, { submitted: true, synced: true, closed: true }),
    observedJob(3, { submitted: true }), observedJob(4, { paused: true, queue_state: "applying" }),
    observedJob(5, { queue_state: "Queued", owner_thread_id: null, turn_state: "not_started", applier_label: "E" }), observedJob(6),
  ] })];
  renderRuns(false, "/runs");
  await screen.findByRole("tab", { name: "Applications (6)" });
  const table = screen.getByRole("table", { name: "Applications" });
  expect(screen.getByRole("tab", { name: "Applications (6)" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "Applying & other (3)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  expect(screen.queryByRole("tab", { name: /Other states/ })).toBeNull();
  expect(within(table).getByTestId("current-run-application-mixed-job-2")).toBeVisible();
  expect(within(table).getByText("Website submission verified · Jobfeed sync pending")).toBeVisible();
  expect(within(table).getByText("Waiting for applier")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Applied" }));
  expect(screen.getByRole("button", { name: "Open Engineer 3 details" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Engineer 4 details" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open Engineer 3 details" }));
  expect(screen.getByLabelText("Current URL")).toHaveTextContent("run=mixed&job=3");
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByRole("heading", { name: "Engineer 3" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
  expect(screen.queryByRole("region", { name: "Selected run details" })).toBeNull();
  expect(screen.getByRole("tab", { name: "Applied" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Applying & other (3)" }));
  expect(screen.getByText("Saved observation · Paused")).toBeVisible();
  expect(screen.getByRole("button", { name: "Open Engineer 1 details" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Open Engineer 6 details" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Engineer 3 details" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Open Engineer 5 details" })).toBeNull();
  expect(calls.filter(call => call.method === "POST")).toEqual([]);
});

test("unbound jobs stay hidden and truncated observations remain explicit", async () => {
  state.batches = [batchRow("unbound", { source_binding: "invalid", jobs: [observedJob(101)], jobs_truncated: true })];
  renderRuns(false, "/runs?kind=application&run=unbound&job=101");
  expect(await screen.findByText("This job is unavailable in the selected batch observation.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Engineer 101 details" })).toBeNull();
  expect(screen.getByText("Only a bounded subset of observed application jobs is shown.")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Open applier chat" })).toBeNull();
});

test("one controller summary accompanies all saved jobs including applied-only observations", async () => {
  const controller = { thread_id: "controller-one", display_name: "Controller · Design applications", dispatch_mode: "controller" as const, max_parallel: 5, worker_status: "stopped_by_user" as const };
  state.batches = [
    batchRow("older", { checked_at: "2026-06-09T10:00:00Z", controller: { ...controller, display_name: "Previous controller name" }, jobs: [observedJob(101, { applier_label: "A", submitted: true })] }),
    batchRow("latest", { controller, jobs: [observedJob(202, { applier_label: "B", submitted: true, synced: true, closed: true })] }),
  ];
  renderRuns(false, "/runs");
  const overview = await screen.findByRole("region", { name: "Applications overview" });
  expect(await within(overview).findByText("Controller · Design applications")).toBeVisible();
  expect(within(overview).queryByText("Previous controller name")).toBeNull();
  expect(within(overview).getByRole("link", { name: "Open controller chat" })).toHaveAttribute("href", "codex://threads/controller-one");
  expect(within(overview).getByText(/Up to 5 concurrent appliers/)).toHaveTextContent("Worker saved status: Stopped by user");
  expect(screen.getByRole("tab", { name: "Applications (2)" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "Applying & other (0)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  expect(within(overview).getByText("Applier A")).toBeVisible();
  expect(within(overview).getByText("Applier B")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Open Engineer 202 details" }));
  expect(screen.getByLabelText("Current URL")).toHaveTextContent("kind=application&run=latest&job=202");
  expect(calls.filter(call => call.method === "POST")).toEqual([]);
});

test("legacy observations keep unknown controller and applier labels explicit", async () => {
  state.batches = [batchRow("legacy", { jobs: [observedJob(101)] })];
  renderRuns(false, "/runs");
  const group = await screen.findByRole("region", { name: "Applications overview" });
  expect(await within(group).findByText("Applier · label not recorded")).toBeVisible();
  expect(screen.queryByText("No active scans or evaluations.")).toBeNull();
  expect(within(group).getByText("No controller is recorded for these saved observations.")).toBeVisible();
  expect(within(group).queryByRole("link", { name: "Open controller chat" })).toBeNull();
  expect(within(group).getByRole("link", { name: "Open applier chat" })).toHaveAttribute("href", "codex://threads/owner-101");
  expect(screen.getByText(/Rerun application replaces the previous form and applier chat/)).toBeVisible();
  expect(within(group).queryByRole("button", { name: /Restart application/ })).toBeNull();
});


test("application runs keep all jobs together and never load scanner data or actions", async () => {
  state.batches = [batchRow("application-only", { jobs: [observedJob(101)] })];
  renderRuns(false, "/runs");
  expect(await screen.findByRole("button", { name: "Open Engineer 101 details" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applications (1)" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Application history" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Scanner history" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Start scan|Start evaluation/ })).toBeNull();
  expect(screen.getByRole("link", { name: "View application queue" })).toHaveAttribute("href", "/runs?tab=queue");
  expect(calls.some(call => call.url.startsWith("/api/runs"))).toBe(false);
  expect(calls.filter(call => call.method === "POST")).toEqual([]);
});

test("scanner runs never render application observations or request application health", async () => {
  state.batches = [batchRow("application-only", { jobs: [observedJob(101)] })];
  renderRuns();
  await screen.findByTestId("run-row-r2");
  expect(screen.queryByRole("button", { name: "Open Engineer 101 details" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Application history" })).toBeNull();
  expect(calls.some(call => call.url === "/api/apply-health")).toBe(false);
});

test("legacy scanner deep links preserve query parameters on the scanner route", async () => {
  renderRuns(false, "/runs?kind=pipeline&run=r2&source=bookmark");
  expect(await screen.findByRole("region", { name: "Selected run details" })).toBeVisible();
  expect(screen.getByLabelText("Current path")).toHaveTextContent("/scanner-runs");
  expect(screen.getByLabelText("Current URL")).toHaveTextContent("?kind=pipeline&run=r2&source=bookmark");
  expect(calls.some(call => call.url === "/api/apply-health")).toBe(false);
});

test("application loading and failure do not masquerade as an empty run list", async () => {
  vi.stubGlobal("fetch", async () => json({ error: { code: "unavailable", message: "Unavailable", request_id: "test" } }, 503));
  renderRuns(false, "/runs");
  expect(screen.queryByText(/No application runs recorded/)).toBeNull();
  expect(await screen.findByText("Apply health unavailable")).toBeVisible();
  expect(screen.queryByText(/No application runs recorded/)).toBeNull();
});


test("source failure with saved jobs is partial and retains the failed source details", async () => {
  state.runs = [runRow("partial-scan", {
    source: "all", status: "failed", failed_stage: "source", failed_source: "handshake",
    jobs_inserted: 716, jobs_discovered: 800, errors: 1,
    failure_message: "Handshake HTTP 401", scan_progress: {
      handshake: { phase: "failed", processed: 0, message: "Handshake authentication expired" },
      indeed: { phase: "completed", processed: 716 },
    },
  })];
  renderRuns();
  const partial = await screen.findByText("Partial");
  expect(partial.closest('[class*="status-warning"]')).not.toBeNull();
  expect(screen.getByTestId("run-activity-partial-scan")).toHaveTextContent("800 discovered · 716 new · 1 errors");
  expect(screen.queryByText("Failed")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Run started/ }));
  expect(screen.getByText("Partial scan: Handshake HTTP 401. 716 new jobs are saved.")).toBeVisible();
  expect(screen.getByText("Handshake authentication expired")).toBeVisible();
  expect(screen.getByText("completed · 716 processed")).toBeVisible();
  expect(calls.some(call => call.method === "POST")).toBe(false);
});

test.each([
  { source: "all", failed_stage: "source", jobs_inserted: 0 },
  { source: "all", failed_stage: "scan", jobs_inserted: 716 },
  { source: "evaluate", failed_stage: "source", jobs_inserted: 716 },
])("keeps a true failed run as Failed for %j", async (overrides) => {
  state.runs = [runRow("failed-scan", { status: "failed", ...overrides })];
  renderRuns();
  expect((await screen.findByText("Failed")).closest('[class*="status-error"]')).not.toBeNull();
  expect(screen.queryByText("Partial")).toBeNull();
});


test("stale execution remains qualified while review, retry, and unverified claims share Applying & other", async () => {
  state.batches = [batchRow("states", { freshness: "stale", jobs: [
    observedJob(1, { queue_state: "Applying" }),
    observedJob(2, { queue_state: "Resume Ready", owner_thread_id: null, turn_state: "not_started" }),
    observedJob(3, { queue_state: "Submitted" }),
    observedJob(4, { user_submitted: true }),
    observedJob(5, { errors: ["submission_unverified"] }),
    observedJob(6, { queue_state: "Ready for Review" }),
    observedJob(7, { queue_state: "Awaiting Approval" }),
    observedJob(8, { queue_state: "Retry Later", owner_thread_id: null }),
    observedJob(9, { queue_state: "Deferred", owner_thread_id: null }),
    observedJob(10, { blocked: "awaiting_user" }),
    observedJob(11, { owner_thread_id: null }),
    observedJob(12, { queue_state: "Applying", turn_state: "ended" }),
    observedJob(13, { turn_state: null }), observedJob(14, { turn_state: "unexpected" }),
  ] })];
  renderRuns(false, "/runs");
  await screen.findByRole("tab", { name: "Applications (14)" });
  expect(screen.getByRole("tab", { name: "Applying & other (13)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Wait" }));
  expect(screen.queryByRole("button", { name: "Open Engineer 2 details" })).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "Applications (14)" }));
  expect(screen.getByRole("button", { name: "Open Engineer 2 details" })).toBeVisible();
  expect(screen.getByText("Waiting for applier")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Applying & other (13)" }));
  expect(screen.getAllByText("Saved observation · stale · Applying")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Open Engineer 1 details" })).toBeVisible();
  expect(screen.getAllByText("Saved observation · stale · Submission unverified")).toHaveLength(3);
  expect(screen.getByRole("button", { name: "Open Engineer 8 details" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Engineer 2 details" })).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "Applied" }));
  expect(screen.getByText("No saved jobs in Applied.")).toBeVisible();
});

test("latest completed observation replaces older active rows with a deterministic equal-time choice", async () => {
  state.batches = [
    batchRow("batch-z", { jobs: [observedJob(1, { title: "Verified result", submitted: true })] }),
    batchRow("batch-a", { jobs: [observedJob(1, { title: "Earlier active row" })] }),
    batchRow("old", { checked_at: "2026-06-09T10:00:00Z", jobs: [observedJob(1, { title: "Old draft" })] }),
  ];
  const { queryClient } = renderRuns(false, "/runs");
  await screen.findByRole("button", { name: "Open Verified result details" });
  expect(screen.getByRole("tab", { name: "Applications (1)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open Earlier active row details" })).toBeNull();
  state.batches.reverse();
  await act(async () => { await queryClient.refetchQueries({ queryKey: ["workspace", "apply-health"] }); });
  expect(screen.getByRole("button", { name: "Open Verified result details" })).toBeVisible();
});

test("application loading has a skeleton without claiming an empty list", async () => {
  vi.stubGlobal("fetch", () => new Promise(() => {}));
  renderRuns(false, "/runs");
  expect(screen.getByRole("table", { name: "Applications" })).toBeVisible();
  expect(screen.getByText("Loading current runs")).toBeInTheDocument();
  expect(screen.queryByText(/No application runs recorded/)).toBeNull();
});


function queueJob(id: number, overrides: Partial<QueueJob> = {}): QueueJob {
  return { job_id: id, title: `Queued engineer ${id}`, company: "Queue Co", queue_state: "Queued", owner_thread_id: null, retry_count: 0, manual_priority: false, ...overrides };
}

function queueRows(jobs: QueueJob[], overrides: Partial<ApplyHealth["queue"]> = {}): ApplyHealth["queue"] {
  return { state: "available", total: jobs.length, jobs, truncated: false, ...overrides };
}

test("queue-only jobs stay out of Wait and appear in All applications without a controller or handoff", async () => {
  state.monitoringState = "missing";
  state.queue = queueRows([queueJob(71), queueJob(72)]);
  const { queryClient } = renderRuns(false, "/runs");
  expect(await screen.findByRole("tab", { name: "Applications (2+)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applying & other (?)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  expect(screen.getByText("Application observations unavailable")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Wait" }));
  expect(screen.queryByTestId("current-run-queue-71")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "Applications (2+)" }));
  const row = screen.getByTestId("current-run-queue-71");
  expect(within(row).getByText("Queued engineer 71")).toBeVisible();
  expect(within(row).getByText("Queued job")).toBeVisible();
  expect(within(row).getByRole("link", { name: "Open application queue" })).toHaveAttribute("href", "/runs?tab=queue");
  expect(within(row).queryByRole("button")).toBeNull();
  expect(screen.getAllByText("Queued · No application run recorded")).toHaveLength(2);
  expect(screen.queryByRole("link", { name: "Open applier chat" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Open controller chat" })).toBeNull();
  expect(screen.queryByText(/Rerun application replaces/)).toBeNull();
  await act(async () => { await queryClient.refetchQueries({ queryKey: ["workspace", "apply-health"] }); });
  expect(screen.getByRole("tab", { name: "Applications (2+)" })).toHaveAttribute("aria-selected", "true");
  expect(calls.every(call => call.method === "GET")).toBe(true);
  expect(calls.filter(call => call.url === "/api/apply-health")).toHaveLength(2);
  expect(calls.some(call => call.url.includes("/handoff"))).toBe(false);
});

test("stored queue rows deduplicate against applied, applying, paused, and waiting observations", async () => {
  state.batches = [batchRow("saved", { freshness: "stale", jobs: [
    observedJob(1, { submitted: true }), observedJob(2), observedJob(3, { paused: true }),
    observedJob(4, { queue_state: "Queued", owner_thread_id: null, turn_state: "not_started" }),
  ] })];
  state.queue = queueRows([1, 2, 3, 4, 5].map(id => queueJob(id)));
  renderRuns(false, "/runs");
  expect(await screen.findByRole("tab", { name: "Applications (5)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applying & other (2)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Applications (5)" }));
  expect(screen.queryByTestId("current-run-queue-1")).toBeNull();
  expect(screen.getByText("Saved observation · stale · Submitted")).toBeVisible();
  expect(screen.getByText("Saved observation · stale · Paused")).toBeVisible();
  expect(screen.getByTestId("current-run-queue-5")).toBeVisible();
  expect(screen.getByText("Waiting for applier")).toBeVisible();
  expect(screen.getByText("Queued job")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "Wait" }));
  expect(screen.queryByText("Waiting for applier")).toBeNull();
});

test("only unowned Queued records are added from the stored queue", async () => {
  state.queue = queueRows([queueJob(1), queueJob(2, { owner_thread_id: "existing-owner" }),
    queueJob(3, { queue_state: "Submitted" }), queueJob(4, { queue_state: "Needs Input" })]);
  renderRuns(false, "/runs");
  expect(await screen.findByRole("tab", { name: "Applications (1)" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Applied" })).toBeVisible();
  expect(screen.queryByText("Queued engineer 2")).toBeNull();
  expect(screen.queryByText("Queued engineer 3")).toBeNull();
  expect(screen.getByText("Queued engineer 1")).toBeVisible();
});

test("unreadable queue data is not shown as an empty queue or trusted as current", async () => {
  state.queue = queueRows([queueJob(1)], { state: "unavailable" });
  renderRuns(false, "/runs");
  expect(await screen.findByText("Application queue unavailable")).toBeVisible();
  expect(screen.getByText(/Wait application totals are unknown/)).toBeVisible();
  expect(screen.queryByText("Queued engineer 1")).toBeNull();
  expect(screen.queryByText(/No saved application runs or unassigned queued jobs/)).toBeNull();
});

test("bounded queue reads label the displayed counts as incomplete", async () => {
  state.queue = queueRows([queueJob(1)], { truncated: true, total: 12 });
  renderRuns(false, "/runs");
  expect(await screen.findByRole("tab", { name: "Wait" })).toBeVisible();
  expect(await screen.findByText(/Only a subset of stored queue jobs is available/)).toBeVisible();
  expect(screen.getByText("Queued engineer 1")).toBeVisible();
});

test("failed refresh retains queue records with an explicit stale-read warning and no handoff", async () => {
  state.queue = queueRows([queueJob(1)]);
  const { queryClient } = renderRuns(false, "/runs");
  await screen.findByRole("tab", { name: "Wait" });
  vi.stubGlobal("fetch", async () => json({ error: { code: "unavailable", message: "Unavailable", request_id: "test" } }, 503));
  await act(async () => { await queryClient.refetchQueries({ queryKey: ["workspace", "apply-health"] }); });
  expect(await screen.findByText("Apply health refresh failed")).toBeVisible();
  expect(screen.getByText(/These observations may now be stale/)).toBeVisible();
  expect(screen.getByText("Queued engineer 1")).toBeVisible();
  expect(calls.filter(call => call.method === "POST")).toEqual([]);
});

test("legacy Other states links and selected other-state jobs open the combined tab", async () => {
  state.batches = [batchRow("mixed", { jobs: [observedJob(1), observedJob(2, { paused: true })] })];
  const { unmount } = renderRuns(false, "/runs?tab=other");
  expect(await screen.findByRole("tab", { name: "Applying & other (2)" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "Open Engineer 1 details" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Open Engineer 2 details" })).toBeVisible();
  unmount();
  renderRuns(false, "/runs?kind=application&run=mixed&job=2");
  expect(await screen.findByRole("tab", { name: "Applying & other (2)" })).toHaveAttribute("aria-selected", "true");
  expect(within(screen.getByRole("region", { name: "Selected run details" })).getByRole("heading", { name: "Engineer 2" })).toBeVisible();
});

test.each(["/runs?tab=results", "/runs?kind=application"])("%s falls back to the Wait tab", async (entry) => {
  renderRuns(false, entry);
  expect(await screen.findByRole("tab", { name: "Wait" })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByRole("tab", { name: "Results" })).toBeNull();
});

test("queue tab keeps the queue table and handoff without the stored-record summary", async () => {
  state.queue = queueRows([queueJob(1)], { counts_by_state: { Queued: 1 } });
  renderRuns(false, "/runs?tab=queue");
  expect(await screen.findByRole("toolbar", { name: "Apply to queued jobs" })).toBeVisible();
  expect(screen.getByRole("table", { name: /Application queue/ })).toHaveTextContent("Queued engineer 1");
  expect(screen.queryByText("Queue status")).toBeNull();
  expect(screen.queryByText("Stored queue records")).toBeNull();
  expect(screen.queryByText(/Totals include all stored states/)).toBeNull();
  expect(screen.queryByRole("list", { name: "Global queue states" })).toBeNull();
  expect(screen.queryByText("Application queue unavailable")).toBeNull();
  expect(screen.queryByText("Apply runtime is not available")).toBeNull();
});

test("queue tab explains an unreadable queue and an unavailable apply runtime", async () => {
  state.queue = queueRows([queueJob(1)], { state: "unavailable" });
  state.runtime = { state: "unavailable", source: "combined_sibling" };
  renderRuns(false, "/runs?tab=queue");
  expect(await screen.findByText("Application queue unavailable")).toBeVisible();
  expect(screen.getByText("No readable queue records are available.")).toBeVisible();
  expect(screen.getByText("Apply runtime is not available")).toBeVisible();
  expect(screen.getByText("The combined workspace has no configured application runtime.")).toBeVisible();
  expect(screen.queryByRole("table", { name: /Application queue/ })).toBeNull();
  expect(screen.queryByText(/Wait application totals are unknown/)).toBeNull();
});
