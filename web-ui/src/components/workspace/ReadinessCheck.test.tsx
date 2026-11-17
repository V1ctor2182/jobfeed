import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReadinessRun } from "@/api/readiness";
import { ReadinessCheck } from "./ReadinessCheck";

const checkedAt = "2026-10-21T16:00:00Z";
function fixture(overrides: Partial<ReadinessRun> = {}): ReadinessRun {
  return {
    run_id: "run-fixture", phase: "completed", started_at: checkedAt, finished_at: checkedAt,
    expires_at: "2026-10-21T16:15:00Z", stale: false, ready: true,
    checks: [{ id: "chrome", label: "Chrome extension", status: "ready", required: true, checked_at: checkedAt, reason: "Local tab connected and PDF uploaded.", next_step: "" }],
    ...overrides,
  };
}

function mount(compact = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><ReadinessCheck compact={compact} /></QueryClientProvider>);
  return client;
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("combined setup shows blockers while keeping diagnostic commands in expandable details", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture({ ready: false, phase: "blocked", checks: [
    { id: "registration", label: "Browser connection", status: "blocked", required: true, checked_at: checkedAt, reason: "The diagnostic chat is not connected.", next_step: "python -m pipeline.application_readiness register" },
  ] }) })));
  mount(true);
  expect(await screen.findByText("Readiness blocked")).toBeVisible();
  expect(screen.getAllByText("The diagnostic chat is not connected.")[0]).toBeVisible();
  const details = screen.getByRole("button", { name: "Diagnostic details" });
  expect(details).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(details);
  expect(details).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("python -m pipeline.application_readiness register")).toBeVisible();
});

test("loading prerequisites disables dispatch until the status and token arrive", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
  mount();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeDisabled();
  expect(screen.getByText("Loading the last readiness check.")).toBeVisible();
  await act(async () => { resolve(Response.json({ csrf_token: "fixture-token", run: null })); });
  expect(await screen.findByText(/No readiness check has run/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeEnabled();
});

test("restores ready results without dispatch and displays required component evidence", async () => {
  const fetch = vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture() }));
  vi.stubGlobal("fetch", fetch); mount();
  expect(await screen.findByText("Required components ready")).toBeVisible();
  const table = screen.getByRole("table", { name: "Application readiness components" });
  expect(within(table).getByText("Chrome extension")).toBeVisible();
  expect(within(table).getByText("Local tab connected and PDF uploaded.")).toBeVisible();
  expect(within(table).getByText("Required")).toBeVisible();
  expect(fetch.mock.calls).toHaveLength(1);
});

test("blocked local checks show their setup commands and browser checks remain not tested", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture({ ready: false, phase: "blocked", checks: [
    { id: "profile", label: "Application profile", status: "blocked", required: true, checked_at: checkedAt, reason: "Profile is missing.", next_step: "python -m pipeline setup" },
    { id: "chrome", label: "Chrome extension", status: "not_tested", required: true, checked_at: null, reason: "Local prerequisites must pass first.", next_step: "Fix the profile and run again." },
  ] }) })));
  mount();
  expect(await screen.findByText("Readiness blocked")).toBeVisible();
  expect(screen.getByText("python -m pipeline setup")).toBeVisible();
  expect(screen.getByText("Not tested")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeEnabled();
});

test("stale ready results are labelled stale and can be checked again", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture({ stale: true }) })));
  mount();
  expect(await screen.findByText("Readiness result is stale")).toBeVisible();
  expect(screen.queryByText("Required components ready")).not.toBeInTheDocument();
  expect(screen.getByText("Run a new check before relying on these results.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeEnabled();
});

test.each(["running", "dispatch_unknown", "cleanup_unknown"] as const)("restoring %s disables duplicate dispatch", async (phase) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture({ phase, ready: false }) })));
  mount();
  await screen.findByRole("table", { name: "Application readiness components" });
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeDisabled();
});

test("a keyboard activation posts once with CSRF and records the returned run", async () => {
  let finish!: (response: Response) => void;
  const requests: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { requests.push(init); return new Promise<Response>((resolve) => { finish = resolve; }); }
    return Response.json({ csrf_token: "fixture-token", run: null });
  }));
  mount();
  await screen.findByText(/No readiness check has run/);
  const button = screen.getByRole("button", { name: "Run readiness check" });
  button.focus();
  expect(button).toHaveFocus();
  fireEvent.keyDown(button, { key: "Enter", code: "Enter" });
  fireEvent.click(button, { detail: 0 });
  fireEvent.click(button, { detail: 0 });
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]?.headers).toEqual({ Accept: "application/json", "Content-Type": "application/json", "X-Readiness-CSRF": "fixture-token" });
  const body = JSON.parse(String(requests[0]?.body)) as { request_id: string };
  expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(button).toBeDisabled();
  await act(async () => { finish(Response.json(fixture())); });
  expect(await screen.findByText("Required components ready")).toBeVisible();
});

test("uncertain POST does not retry or expose raw errors and keeps dispatch disabled", async () => {
  let posts = 0;
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { posts++; throw new Error("PRIVATE_EXCEPTION"); }
    return Response.json({ csrf_token: "fixture-token", run: null });
  }));
  mount(); await screen.findByText(/No readiness check has run/);
  fireEvent.click(screen.getByRole("button", { name: "Run readiness check" }));
  expect(await screen.findByText("Dispatch outcome unknown")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeDisabled();
  expect(screen.queryByText("PRIVATE_EXCEPTION")).not.toBeInTheDocument();
  expect(posts).toBe(1);
});

test("observational GET errors show a safe refresh action without permitting dispatch", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("PRIVATE_EXCEPTION", { status: 503 })));
  mount();
  expect(await screen.findByText("Readiness status unavailable")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Refresh readiness status" })).toBeEnabled();
  expect(screen.queryByText("PRIVATE_EXCEPTION")).not.toBeInTheDocument();
  expect(screen.queryByText(/No readiness check has run/)).not.toBeInTheDocument();
});

test("a restored running check polls until the terminal component result arrives", async () => {
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async () => {
    reads++;
    return Response.json({ csrf_token: "fixture-token", run: fixture(reads === 1 ? { phase: "running", ready: false } : {}) });
  }));
  mount();
  expect(await screen.findByText("Readiness check running")).toBeVisible();
  expect(await screen.findByText("Required components ready", {}, { timeout: 3_000 })).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeEnabled();
});

test("an uncertain dispatch unlocks only after a different terminal run is observed", async () => {
  let posted = false;
  let readsAfterPost = 0;
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { posted = true; throw new Error("lost response"); }
    if (posted) readsAfterPost++;
    return Response.json({ csrf_token: "fixture-token", run: fixture({ run_id: readsAfterPost > 1 ? "new-run" : "old-run" }) });
  }));
  mount(); await screen.findByText("Required components ready");
  fireEvent.click(screen.getByRole("button", { name: "Run readiness check" }));
  expect(await screen.findByText("Dispatch outcome unknown")).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeDisabled();
  expect(screen.getByText("Previous readiness result")).toBeVisible();
  expect(await screen.findByText("Required components ready", {}, { timeout: 3_000 })).toBeVisible();
  expect(screen.getByRole("button", { name: "Run readiness check" })).toBeEnabled();
});

test("a completed result refreshes without dispatch so expired evidence loses ready status", async () => {
  let reads = 0;
  const methods: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    methods.push(init?.method ?? "GET");
    return Response.json({ csrf_token: "fixture-token", run: fixture({ stale: ++reads > 1 }) });
  }));
  vi.useFakeTimers();
  mount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(screen.getByText("Required components ready")).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_100); });
  expect(screen.getByText("Readiness result is stale")).toBeVisible();
  expect(screen.queryByText("Required components ready")).not.toBeInTheDocument();
  expect(methods).toEqual(["GET", "GET"]);
});

test("missing remediation does not imply blocked prerequisites or per-job checks need no action", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ csrf_token: "fixture-token", run: fixture({ ready: false, phase: "blocked", checks: [
    { id: "profile", label: "Application profile", status: "blocked", required: true, checked_at: checkedAt, reason: "Create the application profile.", next_step: "" },
    { id: "login", label: "Employer login", status: "not_tested", required: false, checked_at: null, reason: "Each employer may require a login.", next_step: "" },
    { id: "browser", label: "Browser connection", status: "not_tested", required: true, checked_at: null, reason: "Fix local prerequisites first.", next_step: "" },
    { id: "runtime", label: "Python runtime", status: "ready", required: true, checked_at: checkedAt, reason: "Runtime available.", next_step: "" },
  ] }) })));
  mount();
  const table = await screen.findByRole("table", { name: "Application readiness components" });
  const profile = within(table).getByText("Application profile").closest("tr")!;
  const login = within(table).getByText("Employer login").closest("tr")!;
  const runtime = within(table).getByText("Python runtime").closest("tr")!;
  const browser = within(table).getByText("Browser connection").closest("tr")!;
  expect(within(profile).getByText("Follow result instructions, then run the check again.")).toBeVisible();
  expect(within(browser).getByText("Follow result instructions, then run the check again.")).toBeVisible();
  expect(within(login).getByText("Check when the batch or job runs.")).toBeVisible();
  expect(within(login).getByText("Per batch or job")).toBeVisible();
  expect(within(runtime).getByText("No action needed.")).toBeVisible();
});
