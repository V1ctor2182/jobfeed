import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import type { ApplicationQueueControl, QueueAdmissionReceipt } from "@/api/application-queue";
import { DensityProvider } from "@/lib/density";
import TriagePage from "./triage";
import RunsPage from "./runs";
import ApplicationQueuePage from "./application-queue";

const posts: { request_id: string; real_job_ids: string[] }[] = [];
const reads: string[] = [];
const allIds = ["canonical-a", "canonical-b", "canonical-off-page"];

function receipt(ids: string[] = allIds, requestId = "known-request"): QueueAdmissionReceipt {
  return { request_id: requestId, intent: "queue", status: "queued", plan_id: null, reason: null, submit_policy: "stop_before_submit", runtime: "codex",
    items: ids.map((id, index) => ({ real_job_id: id, source_job_id: index + 11, status: "queued", reason: null })),
  };
}

function mockQueue(options: { rejectPost?: boolean; partial?: boolean; allIds?: string[]; requests?: QueueAdmissionReceipt[]; consumer?: ApplicationQueueControl["consumer"] } = {}) {
  let requests = options.requests ?? [];
  const matching = options.allIds ?? allIds;
  const queued = new Set<string>();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/application-queue" && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      posts.push(body);
      expect(new Headers(init.headers).get("X-Application-Queue-CSRF")).toBe("queue-csrf");
      if (options.rejectPost) throw new Error("connection lost");
      const saved = receipt(body.real_job_ids, body.request_id);
      if (options.partial && saved.items[1]) saved.items[1] = { ...saved.items[1], status: "rejected", reason: "This job is no longer in Wait." };
      requests = [saved];
      saved.items.filter(item => item.status !== "rejected").forEach(item => queued.add(item.real_job_id!));
      return Response.json(saved);
    }
    expect(init?.method ?? "GET").toBe("GET");
    reads.push(url);
    if (url.startsWith("/api/application-queue")) return Response.json({ csrf_token: "queue-csrf", requests, consumer: options.consumer ?? { state: "idle", reason: null } });
    if (url.startsWith("/api/real-jobs/selection?")) return Response.json({ real_job_ids: matching, total: matching.length });
    const waiting = matching.filter(id => !queued.has(id));
    if (url.startsWith("/api/real-jobs?")) return Response.json({ jobs: allIds.slice(0, 2).map((id, index) => ({
      id, real_job_id: id, source_job_id: String(index + 101), company: `Company ${index + 1}`, title: "Engineer", location: "Remote",
      status: "shortlisted", decision: "wait", verdict: "apply", stage_a_score: 80, stage_b_fit_score: 90, stage_b_status: "completed",
      identity_review_state: "clear", discovered_at: "2026-10-23T12:00:00Z", evaluation_stale_reason: null,
    })).filter(job => !queued.has(job.id)), total: waiting.length, total_is_exact: true, tab_counts: { wait: waiting.length } });
    if (url.startsWith("/api/real-jobs/")) return new Promise<Response>(() => {});
    if (url.startsWith("/api/job-stars?")) return Response.json({ stars: [] });
    if (url === "/api/apply-health") return Response.json({
      mode: "read_only", checked_at: "2026-10-23T12:00:00Z", runtime: { state: "available", source: "combined_sibling" },
      queue: { state: "available", total: 0, jobs: [], counts_by_state: {}, truncated: false },
      monitoring: { state: "available", discovered_batches: 0, truncated: false }, batches: [],
    });
    throw new Error(`Unexpected request ${url}`);
  }));
}

function NavigationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output aria-label="Current URL">{location.pathname}{location.search}{location.hash}</output><button onClick={() => navigate(-1)}>Back</button><button onClick={() => navigate(1)}>Forward</button></>;
}

function renderFlow(path = "/triage?tab=wait") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><DensityProvider>
    <NavigationProbe /><Routes><Route path="/runs" element={<RunsPage />} /><Route path="/triage" element={<TriagePage />} /><Route path="/application-queue" element={<ApplicationQueuePage />} /></Routes>
  </DensityProvider></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => { posts.length = 0; reads.length = 0; sessionStorage.clear(); mockQueue(); });
afterEach(() => { vi.unstubAllGlobals(); });

test("Wait enqueues all selected canonical IDs including off-page jobs and removes them from the Wait list", async () => {
  renderFlow();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  fireEvent.click(screen.getByRole("button", { name: "Select all 3 results" }));
  expect(await screen.findByText("3 selected")).toBeVisible();
  const enqueue = screen.getByRole("button", { name: "Add to application queue" });
  await waitFor(() => expect(enqueue).toBeEnabled());
  fireEvent.click(enqueue);
  expect(await screen.findByText("Selected jobs are in the application queue")).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0]).toEqual({ request_id: expect.any(String), real_job_ids: ["canonical-a", "canonical-b", "canonical-off-page"] });
  await waitFor(() => expect(screen.queryByTestId("job-row-canonical-a")).not.toBeInTheDocument());
  expect(screen.queryByTestId("job-row-canonical-b")).not.toBeInTheDocument();
  expect(screen.queryByText("3 selected")).not.toBeInTheDocument();
  expect(screen.getByText("Queued jobs moved to the Application queue tab. No applications were started.")).toBeVisible();
  for (const link of screen.getAllByRole("link", { name: "View application queue" })) expect(link).toHaveAttribute("href", "/runs?tab=queue");
  expect(reads.some(url => url.includes("/selection?") && url.includes("decision=wait"))).toBe(true);
});

test("partial admission clears only successful selections and shows each rejection", async () => {
  mockQueue({ partial: true });
  renderFlow();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Select Company 2 Engineer" }));
  const enqueue = screen.getByRole("button", { name: "Add to application queue" });
  await waitFor(() => expect(enqueue).toBeEnabled());
  fireEvent.click(enqueue);
  expect(await screen.findByText("Some jobs were not queued")).toBeVisible();
  await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Select Company 1 Engineer" })).not.toBeInTheDocument());
  expect(screen.getByRole("checkbox", { name: "Select Company 2 Engineer" })).toBeChecked();
  expect(screen.getByText("1 selected")).toBeVisible();
  expect(screen.getByText(/This job is no longer in Wait/)).toBeVisible();
});

test("an unknown POST is never repeated during status checks or after remount", async () => {
  mockQueue({ rejectPost: true });
  const first = renderFlow();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  const enqueue = screen.getByRole("button", { name: "Add to application queue" });
  await waitFor(() => expect(enqueue).toBeEnabled());
  fireEvent.click(enqueue);
  expect(await screen.findByText("Queue request delivery unconfirmed")).toBeVisible();
  const requestId = posts[0]!.request_id;
  fireEvent.click(screen.getByRole("button", { name: "Check queue status" }));
  expect(enqueue).toBeDisabled();
  first.unmount();
  renderFlow();
  expect(await screen.findByText("Queue request delivery unconfirmed")).toBeVisible();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  expect(screen.getByRole("button", { name: "Add to application queue" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check queue status" }));
  await waitFor(() => expect(reads.some(url => url.includes(`request_id=${requestId}`))).toBe(true));
  expect(posts).toHaveLength(1);
});

test("reconciles a previously unknown request by reading its saved receipt without a second POST", async () => {
  sessionStorage.setItem("jobfeed-application-queue-attempt", JSON.stringify({ requestId: "known-request", realJobIds: ["canonical-a"], state: "uncertain" }));
  mockQueue({ requests: [receipt(["canonical-a"])] });
  renderFlow();
  expect(await screen.findByText("Selected jobs are in the application queue")).toBeVisible();
  await waitFor(() => expect(sessionStorage.getItem("jobfeed-application-queue-attempt")).toBeNull());
  await waitFor(() => expect(reads.filter(url => url.startsWith("/api/real-jobs?"))).toHaveLength(2));
  expect(posts).toHaveLength(0);
});

test("a selection over the request limit remains visible and cannot be silently truncated", async () => {
  mockQueue({ allIds: Array.from({ length: 101 }, (_, index) => `canonical-${index}`) });
  renderFlow();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  fireEvent.click(screen.getByRole("button", { name: "Select all 101 results" }));
  expect(await screen.findByText("101 selected")).toBeVisible();
  expect(screen.getByText(/Choose up to 100 jobs/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Add to application queue" })).toBeDisabled();
  expect(posts).toHaveLength(0);
});

test("queue tab owns its URL, resets job selection, and follows browser back and forward", async () => {
  renderFlow();
  fireEvent.click(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" }));
  fireEvent.click(screen.getByRole("tab", { name: "Application queue" }));
  expect(await screen.findByRole("toolbar", { name: "Apply to queued jobs" })).toBeVisible();
  expect(screen.getByRole("status", { name: "Current URL" })).toHaveTextContent("/runs?tab=queue");
  expect(screen.queryByRole("region", { name: "Job detail" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" })).not.toBeChecked();
  expect(screen.getByRole("tab", { name: "Wait" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("button", { name: "Forward" }));
  expect(await screen.findByRole("toolbar", { name: "Apply to queued jobs" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Application queue" })).toHaveAttribute("aria-selected", "true");
});

test("old queue links redirect to Triage without starting controllers or displaying automatic preparation", async () => {
  renderFlow("/application-queue");
  expect(await screen.findByRole("toolbar", { name: "Apply to queued jobs" })).toBeVisible();
  expect(screen.getByRole("status", { name: "Current URL" })).toHaveTextContent("/runs?tab=queue");
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  expect(screen.getByText(/Adding jobs does not start applications/)).toBeVisible();
  expect(screen.queryByText("Automatic preparation")).not.toBeInTheDocument();
  expect(posts).toHaveLength(0);
  expect(reads.some(url => url.startsWith("/api/real-jobs"))).toBe(false);
});


test("legacy Triage links preserve the decision tab, other parameters, and fragment", async () => {
  renderFlow("/triage?tab=ignored&sort=score_desc#saved");
  expect(await screen.findByRole("checkbox", { name: "Select Company 1 Engineer" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Ignored" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("status", { name: "Current URL" })).toHaveTextContent("/runs?tab=ignored&sort=score_desc#saved");
  expect(posts).toHaveLength(0);
});
