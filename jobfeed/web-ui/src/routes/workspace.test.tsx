import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import App from "@/App";
import { configurationKey } from "@/api/configuration";
import type { ApplyHealth, BatchHealth } from "@/api/workspace";
import { DensityProvider } from "@/lib/density";

const CHECKED_AT = "2026-10-21T16:00:00Z";
const calls: string[] = [];
const overview = { window_days: null, totals: { jobs: 42, evaluated: 12, detailed_reviewed: 7, ml_gate_passed: 20, applied: 0 }, daily: [], decision_distribution: {}, verdict_distribution: {}, applied_by_source: { ashby: 3, indeed: 2 } };

function batch(id: string, submitted: number): BatchHealth {
  return {
    id, jobs_truncated: false, source_binding: "valid", freshness: "fresh", checked_at: CHECKED_AT,
    counts: { submitted, synced: 1, closed: 0, reserved: 0, occupied: 1, paused: 0 },
    blockers: { pending_controller: 0, awaiting_user: 1, waiting_result: 0 },
    queue_health: { state: "no_active_issue_recorded" },
    jobs: [{ job_id: 11, company: "Orchard", title: "Product designer", queue_state: "Needs Input", blocked: "awaiting_user", submitted: false, counted: true, terminal: false, user_submitted: false, synced: false, closed: false, paused: false, last_tool_at: CHECKED_AT }],
  };
}

function fixture(): ApplyHealth {
  return {
    checked_at: CHECKED_AT, mode: "read_only",
    runtime: { state: "available", source: "combined_sibling" },
    queue: { state: "available", truncated: false, total: 2, counts_by_state: { queued: 1, needs_input: 1 }, jobs: [
      { job_id: 11, title: "Product designer", company: "Orchard", queue_state: "Needs Input", retry_count: 0, owner_thread_id: "thread-11", manual_priority: true },
      { job_id: 12, title: "Platform engineer", company: "Harbor", queue_state: "Queued", retry_count: 1, manual_priority: false },
    ] },
    monitoring: { state: "available", discovered_batches: 2, truncated: false },
    batches: [batch("batch-current", 2), batch("batch-older", 19)],
  };
}

function postingDetail() {
  return {
    job: {
      id: "12", canonical_id: "harbor-12", company: "Harbor", title: "Platform engineer", location: "Lisbon, Portugal",
      platform: "greenhouse", url: "https://boards.greenhouse.io/harbor/jobs/12", posted_at: null,
      discovered_at: "2026-06-16T00:00:00Z", closed_at: null, jd_quality: "full", jd_text: "Run the platform team's build tooling.",
    },
    evaluation: { stage_a: null, stage_b_status: null, stage_b: null },
    status: { status: "new", decision: "wait", history: [], notes: null, next_followup_at: null, resume_variant: null },
    twins: [], interviews: [], application: null,
  };
}

function mockApi(data: ApplyHealth, config: "pending" | "error" | "unconfigured" | "ready" = "ready", applyFails = () => false) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push(url);
    expect(init?.method ?? "GET").toBe("GET");
    if (url === "/api/config") {
      if (config === "pending") return new Promise<Response>(() => {});
      if (config === "error") return new Response("unavailable", { status: 500 });
      return Response.json({ configured: config === "ready" });
    }
    if (url.startsWith("/api/application-queue")) return Response.json({ csrf_token: "queue-csrf", requests: [], consumer: { state: "idle", reason: null } });
    if (url.startsWith("/api/real-jobs?")) return Response.json({ jobs: [], total: 0, total_is_exact: true, tab_counts: {} });
    if (url === "/api/apply-health") return applyFails() ? new Response("unavailable", { status: 503 }) : Response.json(data);
    if (/^\/api\/apply-health\/jobs\/\d+\/confirmation$/.test(url)) return Response.json({ status: "unverified" });
    if (url === "/api/application-tabs") return Response.json({ csrf_token: "fixture-csrf", bindings: [] });
    if (url === "/api/apply-readiness") return Response.json({ csrf_token: "fixture-csrf", run: null });
    if (url === "/api/health") return Response.json({ status: "ok", db: "ok" });
    if (url.startsWith("/api/insights/overview")) return Response.json(overview);
    if (url === "/api/jobs/12") return Response.json(postingDetail());
    throw new Error(`Unexpected request ${url}`);
  }));
}

function renderWorkspace(path = "/dashboard", configured?: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (configured !== undefined) client.setQueryData(configurationKey, { configured });
  render(<QueryClientProvider client={client}><DensityProvider><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></DensityProvider></QueryClientProvider>);
  return client;
}

beforeEach(() => { calls.length = 0; window.localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });

test.each(["pending", "error", "unconfigured"] as const)("overview is accessible while provider config is %s", async (config) => {
  mockApi(fixture(), config);
  renderWorkspace("/", config === "unconfigured" ? false : undefined);
  expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  expect(await screen.findByText("42")).toBeVisible();
  expect(calls).not.toContain("/api/personal-ml/status");
});

test.each(["pending", "error", "unconfigured"] as const)("apply health is accessible while provider config is %s", async (config) => {
  mockApi(fixture(), config);
  renderWorkspace("/apply-health", config === "unconfigured" ? false : undefined);
  expect(await screen.findByRole("heading", { level: 1, name: "Apply health" })).toBeVisible();
  expect(await screen.findByRole("heading", { name: /Application queue/ })).toBeVisible();
});

test("existing Jobfeed routes retain the settings loading gate", async () => {
  mockApi(fixture(), "pending");
  renderWorkspace("/library");
  expect(screen.getByText("Loading settings")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Triage" })).not.toBeInTheDocument();
});


test("missing queue and monitor show unknown counts", async () => {
  const data = fixture();
  data.queue = { state: "missing", truncated: false, total: null };
  data.monitoring = { state: "missing", discovered_batches: 0, truncated: false };
  data.batches = [];
  mockApi(data);
  renderWorkspace("/apply-health");
  expect(await screen.findByText("Application queue is not initialized")).toBeVisible();
  expect(screen.getByText("Unknown")).toBeVisible();
  expect(screen.getByText("No current monitoring evidence")).toBeVisible();
  expect(screen.queryByText("No application jobs are queued.")).not.toBeInTheDocument();
});

test("valid empty queue shows zero and an explicit empty state", async () => {
  const data = fixture(); data.queue = { state: "available", truncated: false, total: 0, jobs: [] }; data.batches = [];
  mockApi(data); renderWorkspace("/apply-health");
  expect((await screen.findAllByText("No application jobs are queued."))[0]).toBeVisible();
  expect(screen.getAllByText("0")[0]).toBeVisible();
  expect(screen.queryByText("Unknown")).not.toBeInTheDocument();
});

test("stale batch and invalid binding remain visible alongside incident evidence", async () => {
  const data = fixture(); const observed = data.batches![0]!;
  observed.freshness = "stale"; observed.source_binding = "invalid"; observed.issues = ["manifest_changed"];
  observed.queue_health = { state: "active_issue", error_type: "database_locked", first_seen: CHECKED_AT, last_seen: CHECKED_AT, delivery_state: "pending" };
  mockApi(data); renderWorkspace("/apply-health");
  expect(await screen.findByText("Observation stale")).toBeVisible();
  expect(screen.getByText("manifest changed")).toBeVisible();
  expect(screen.getByText("Queue health incident")).toBeVisible();
  expect(screen.getByText("database locked")).toBeVisible();
});

test("selecting another batch changes counts without summing history", async () => {
  mockApi(fixture()); renderWorkspace("/apply-health");
  await screen.findByText("Recently observed");
  expect(screen.queryByText("19")).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("button", { name: /Batch/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: /batch-older/ }));
  expect(await screen.findByText("19")).toBeVisible();
  expect(screen.queryByText("21")).not.toBeInTheDocument();
});

test.each(["/apply-health", "/application-queue"])("queue search, state filter, and details show the selected source on %s", async (path) => {
  mockApi(fixture()); renderWorkspace(path);
  const table = await screen.findByRole("table", { name: /Application queue/ });
  fireEvent.change(screen.getByRole("searchbox", { name: "Search queue" }), { target: { value: "Harbor" } });
  expect(within(table).getByRole("button", { name: "Platform engineer" })).toBeVisible();
  expect(within(table).queryByRole("button", { name: "Product designer" })).not.toBeInTheDocument();
  fireEvent.click(within(table).getByRole("button", { name: "Platform engineer" }));
  const dialog = screen.getByRole("dialog", { name: "Job details" });
  expect(dialog).toBeVisible();
  expect(await within(dialog).findByText("Lisbon, Portugal")).toBeVisible();
  expect(within(dialog).getByText("Run the platform team's build tooling.")).toBeVisible();
  expect(within(dialog).getByText("Source ID").nextElementSibling).toHaveTextContent("12");
  expect(within(table).getByRole("button", { name: "Platform engineer" })).toHaveAttribute("aria-expanded", "true");
  expect(within(table).getByRole("button", { name: "Platform engineer" })).toHaveAttribute("aria-controls", "queue-job-details-12");
  fireEvent.click(within(dialog).getByRole("button", { name: "Close details" }));
  expect(screen.queryByRole("dialog", { name: "Job details" })).toBeNull();
  expect(within(table).getByRole("button", { name: "Platform engineer" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.change(screen.getByRole("searchbox", { name: "Search queue" }), { target: { value: "" } });
  fireEvent.mouseDown(screen.getByRole("button", { name: /Queue state/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "Needs Input" }));
  expect(within(table).queryByRole("button", { name: "Platform engineer" })).not.toBeInTheDocument();
  expect(within(table).getByRole("button", { name: "Product designer" })).toBeVisible();
});

test.each(["/apply-health", "/application-queue"])("refresh errors preserve the last useful snapshot and label it stale on %s", async (path) => {
  let failed = false;
  mockApi(fixture(), "ready", () => failed);
  renderWorkspace(path);
  await screen.findByRole("heading", { level: 2, name: /Application queue/ });
  failed = true; fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("Apply health refresh failed")).toBeVisible();
  expect(screen.getByText(/These observations may now be stale/)).toBeVisible();
  expect(screen.getByRole("heading", { level: 2, name: /Application queue/ })).toBeVisible();
  await waitFor(() => expect(calls.filter((url) => url === "/api/apply-health")).toHaveLength(2));
});



test("actual queue state labels flag attention and preserve user confirmed submission", async () => {
  const data = fixture();
  const observed = data.batches![0]!.jobs![0]!;
  observed.queue_state = "Submission Unknown";
  observed.blocked = null;
  observed.user_submitted = true;
  mockApi(data); renderWorkspace();
  const table = await screen.findByRole("table", { name: /Needs attention/ });
  fireEvent.click(within(table).getByRole("button", { name: "Product designer" }));
  await waitFor(() => expect(screen.queryByText("Loading confirmation evidence…")).not.toBeInTheDocument());
  expect(screen.getByText("Confirmation evidence").nextElementSibling).toHaveTextContent("Submission unverified");
  expect(screen.getByText("User confirmed submission").nextElementSibling).toHaveTextContent("Yes");
});


test("metadata errors preserve queue counts and show partial evidence", async () => {
  const data = fixture(); data.queue.error = "job_metadata_unavailable";
  mockApi(data); renderWorkspace("/apply-health");
  expect(await screen.findByText("Queue details are incomplete")).toBeVisible();
  expect(screen.getByText(/Queue counts remain available/)).toBeVisible();
  expect(screen.getByRole("heading", { level: 2, name: /Application queue/ })).toBeVisible();
});


test("stalled activity reason takes priority over the ordinary next step", async () => {
  const data = fixture(); const observed = data.batches![0]!.jobs![0]!;
  observed.queue_state = "Applying"; observed.blocked = null;
  observed.next_step = "wait_for_result";
  observed.errors = ["no_recent_tool_activity_for_600_seconds"];
  mockApi(data); renderWorkspace();
  const table = await screen.findByRole("table", { name: /Needs attention/ });
  expect(within(table).getByText("no recent tool activity for 600 seconds")).toBeVisible();
  expect(within(table).queryByText("wait for result")).not.toBeInTheDocument();
});


test.each(["/dashboard/", "/apply-health/"])("trailing slash monitoring route %s bypasses provider setup", async (path) => {
  mockApi(fixture(), "unconfigured"); renderWorkspace(path, false);
  expect(await screen.findByRole("heading", { level: 1, name: path === "/dashboard/" ? "Dashboard" : "Apply health" })).toBeVisible();
  expect(calls).not.toContain("/api/personal-ml/status");
  if (path === "/apply-health/") {
    expect(await screen.findByText("Recently observed")).toBeVisible();
    expect(screen.getByText("Tab closure verified")).toBeVisible();
  }
});


test.each(["pending", "error", "unconfigured"] as const)("application queue and runs remain accessible while provider config is %s", async (config) => {
  mockApi(fixture(), config);
  renderWorkspace("/application-queue", config === "unconfigured" ? false : undefined);
  expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  expect(await screen.findByRole("table", { name: /Application queue/ })).toHaveTextContent("Platform engineer");
  expect(screen.queryByText("Stored queue records")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: /^Applications/ }));
  expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  expect(await screen.findByRole("button", { name: "Open Product designer details" })).toBeVisible();
  expect(calls.some(url => url.startsWith("/api/runs"))).toBe(false);
  expect(calls).not.toContain("/api/personal-ml/status");
});

test.each(["pending", "error", "unconfigured"] as const)("Triage queue deep link remains accessible while provider config is %s", async (config) => {
  mockApi(fixture(), config);
  renderWorkspace("/runs?tab=queue", config === "unconfigured" ? false : undefined);
  expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  expect(await screen.findByRole("table", { name: /Application queue/ })).toHaveTextContent("Platform engineer");
  expect(screen.getByRole("tab", { name: "Application queue" })).toHaveAttribute("aria-selected", "true");
  expect(calls).not.toContain("/api/personal-ml/status");
  expect(calls.some(url => url.startsWith("/api/real-jobs"))).toBe(false);
});

test.each(["/no-such-page", "/triage", "/triage?tab=results"])("%s lands on the Application runs Wait tab", async (path) => {
  mockApi(fixture()); renderWorkspace(path);
  expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Wait" })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByRole("tab", { name: "Results" })).toBeNull();
});


test("apply health queue totals include all stored states and keep bounded rows explicit", async () => {
  const data = fixture();
  data.queue = { ...data.queue, total: 203, truncated: true, counts_by_state: { Queued: 200, Applied: 3 } };
  mockApi(data); renderWorkspace("/apply-health");
  expect(await screen.findByText("203")).toBeVisible();
  expect(screen.getByText(/Totals include all stored states, including completed records/)).toBeVisible();
  expect(screen.getByRole("list", { name: "Global queue states" })).toHaveTextContent("200 Queued");
  expect(screen.getByRole("list", { name: "Global queue states" })).toHaveTextContent("3 Applied");
  expect(screen.getByText(/Showing a bounded subset of the global queue/)).toBeVisible();
});

test.each(["missing", "unavailable"] as const)("%s queue does not display a false empty table", async (state) => {
  const data = fixture(); data.queue = { state, total: null, truncated: false };
  mockApi(data); renderWorkspace("/application-queue");
  expect(await screen.findByText(state === "missing" ? "Application queue is not initialized" : "Application queue unavailable")).toBeVisible();
  expect(screen.getByText("No readable queue records are available.")).toBeVisible();
  expect(screen.queryByRole("table", { name: /Application queue/ })).toBeNull();
  expect(screen.queryByText("No application jobs are queued.")).toBeNull();
});

test("an empty readable application queue shows an explicit empty table", async () => {
  const data = fixture(); data.queue = { state: "available", total: 0, truncated: false, jobs: [] };
  mockApi(data); renderWorkspace("/application-queue");
  expect(await screen.findByRole("table", { name: /Application queue/ })).toHaveTextContent("No application jobs are queued.");
});
