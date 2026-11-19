import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { QueueAdmissionReceipt } from "@/api/application-queue";
import type { QueueJob } from "@/api/workspace";
import { ApplicationTable } from "./ApplicationTable";

const jobs: QueueJob[] = [
  { job_id: 21, company: "Orchard", title: "Designer", queue_state: "Queued", retry_count: 0, manual_priority: true },
  { job_id: 11, company: "Harbor", title: "Engineer", queue_state: "Queued", retry_count: 0, manual_priority: false },
  { job_id: 31, company: "Assigned", title: "Analyst", queue_state: "Queued", retry_count: 0, owner_thread_id: "existing-applier", manual_priority: false },
  { job_id: 41, company: "Finished", title: "Architect", queue_state: "Applied", retry_count: 0, manual_priority: false },
];
type HandoffBody = { request_id: string; selection: { kind: string; source_job_ids: number[] }; authorize_dispatch: boolean; runtime?: "codex" | "claude" };
const posts: HandoffBody[] = [];
const reads: string[] = [];

function receipt(requestId: string, ids = [21, 11], status: QueueAdmissionReceipt["status"] = "starting"): QueueAdmissionReceipt {
  return { request_id: requestId, intent: "handoff", status, plan_id: "plan-one", reason: null, submit_policy: "stop_before_submit", controller_thread_id: "controller-one", runtime: "codex",
    items: ids.map(id => ({ real_job_id: `canonical-${id}`, source_job_id: id, status: "queued", reason: null })),
  };
}

function mockApi(options: { unknown?: boolean; state?: QueueAdmissionReceipt["status"]; partial?: boolean; saved?: QueueAdmissionReceipt } = {}) {
  let saved = options.saved ?? null;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") {
      expect(url).toBe("/api/application-queue/handoff");
      expect(new Headers(init.headers).get("X-Application-Queue-CSRF")).toBe("handoff-csrf");
      const body: HandoffBody = JSON.parse(String(init.body));
      posts.push(body);
      if (options.unknown) throw new Error("connection lost");
      saved = { ...receipt(body.request_id, body.selection.source_job_ids, options.state), runtime: body.runtime ?? "codex" };
      if (options.partial && saved.items[1]) saved.items[1] = { ...saved.items[1], status: "rejected", reason: "An applier was already assigned." };
      return Response.json(saved);
    }
    expect(init?.method ?? "GET").toBe("GET");
    reads.push(url);
    if (url.startsWith("/api/application-queue/handoff?")) return Response.json({ request: saved });
    if (url === "/api/application-queue") return Response.json({ csrf_token: "handoff-csrf", requests: [], consumer: { state: "idle", reason: null } });
    if (url.startsWith("/api/job-stars?")) return Response.json({ stars: [] });
    if (url.includes("/confirmation")) return Response.json({ status: "unverified" });
    throw new Error(`Unexpected request ${url}`);
  }));
}

function renderQueue() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><ApplicationTable jobs={jobs} enableHandoff /></MemoryRouter>
  </QueryClientProvider>);
}

async function selectBothAndReview() {
  fireEvent.click(screen.getByRole("checkbox", { name: "Select queued Orchard Designer" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Select queued Harbor Engineer" }));
  const button = screen.getByRole("button", { name: "Apply" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.getByRole("dialog");
}

const applied = (ids = [21, 11]) => ({ request_id: expect.any(String), selection: { kind: "source", source_job_ids: ids }, authorize_dispatch: true, runtime: "claude" });

beforeEach(() => { sessionStorage.clear(); posts.length = 0; reads.length = 0; mockApi(); });
afterEach(() => { vi.unstubAllGlobals(); });

test("queue selection excludes owned and completed jobs, survives filtering, and requires one exact review", async () => {
  renderQueue();
  expect(screen.queryByRole("button", { name: "Hand over to controller" })).toBeNull();
  expect(screen.getByRole("checkbox", { name: "Select queued Assigned Analyst" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "Select queued Finished Architect" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "Select queued Orchard Designer" }));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search queue" }), { target: { value: "Harbor" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "Select queued Harbor Engineer" }));
  expect(screen.getByText("2 selected")).toBeVisible();
  const trigger = screen.getByRole("button", { name: "Apply" });
  await waitFor(() => expect(trigger).toBeEnabled());
  fireEvent.click(trigger);
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText("Apply to 2 jobs")).toBeVisible();
  expect(within(dialog).getByRole("list", { name: "Jobs to apply to" })).toHaveTextContent("Orchard · Designer · Source 21");
  expect(within(dialog).getByRole("list", { name: "Jobs to apply to" })).toHaveTextContent("Harbor · Engineer · Source 11");
  for (const words of [/fills each form in your Chrome and stops before Submit/, /Its questions arrive in Discord/,
    /You confirm each Submit in Application runs/, /One application worker runs at a time/]) {
    expect(within(dialog).getByText(words)).toBeVisible();
  }
  expect(within(dialog).queryByText(/controller/i)).toBeNull();
  expect(posts).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByText("2 selected")).toBeVisible();
  expect(posts).toHaveLength(0);
});

test("Confirm and apply sends only reviewed source IDs once with the Claude runtime and clears acknowledged jobs", async () => {
  mockApi({ state: "preparing" });
  renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText("Apply status")).toBeVisible();
  expect(posts).toEqual([applied()]);
  const status = screen.getByText(/Claude Appliers are filling these forms in your Chrome/);
  expect(status).toHaveTextContent("Their questions arrive in Discord.");
  expect(screen.getByText("0 selected")).toBeVisible();
  expect(screen.queryByRole("link", { name: /controller chat/ })).toBeNull();
  expect(screen.getByRole("link", { name: "View application runs" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  expect(posts).toHaveLength(1);
});

test("partial dispatch retains rejected rows selected", async () => {
  mockApi({ partial: true });
  renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText(/An applier was already assigned/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Select queued Orchard Designer" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select queued Harbor Engineer" })).toBeChecked();
});

test.each(["waiting_for_capacity", "needs_attention", "queued"] as const)("%s retains selection without automatically trying again", async state => {
  mockApi({ state });
  renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText("Apply status")).toBeVisible();
  expect(screen.getByText("2 selected")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  expect(posts).toHaveLength(1);
});

test("unknown delivery survives remount and only reconciles through GET", async () => {
  mockApi({ unknown: true });
  const original = renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText("Request unconfirmed")).toBeVisible();
  const requestId = posts[0]!.request_id;
  expect(screen.getByText("2 selected")).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  original.unmount();
  renderQueue();
  expect(await screen.findByText("Request unconfirmed")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  await waitFor(() => expect(reads.some(url => url.includes(`request_id=${requestId}`))).toBe(true));
  expect(posts).toHaveLength(1);
});

test("a Codex request saved before the switch still shows its controller status without resending", async () => {
  sessionStorage.setItem("jobfeed-controller-handoff", JSON.stringify({ requestId: "saved-handoff", jobIds: [21], state: "uncertain" }));
  mockApi({ saved: receipt("saved-handoff", [21], "preparing") });
  renderQueue();
  expect(await screen.findByText(/Jobs handed to the controller/)).toBeVisible();
  expect(screen.getByText("Controller handoff status")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open controller chat" })).toHaveAttribute("href", "codex://threads/controller-one");
  expect(posts).toHaveLength(0);
});

test("a blocked request retains exact selections without resending", async () => {
  mockApi({ state: "waiting_for_capacity" });
  renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText(/These jobs have not started. This will not retry automatically/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Select queued Orchard Designer" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Select queued Harbor Engineer" })).toBeChecked();
  expect(screen.queryByRole("link", { name: /controller chat/ })).toBeNull();
  expect(posts).toEqual([applied()]);
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  await waitFor(() => expect(reads.some(url => url.includes(`request_id=${posts[0]!.request_id}`))).toBe(true));
  expect(posts).toHaveLength(1);
});

test("applying after an acknowledged Codex request replaces its status with the Claude status", async () => {
  sessionStorage.setItem("jobfeed-controller-handoff", JSON.stringify({ requestId: "previous-handoff", jobIds: [99], state: "acknowledged" }));
  mockApi({ saved: receipt("previous-handoff", [99], "preparing"), state: "preparing" });
  renderQueue();
  expect(await screen.findByRole("link", { name: "Open controller chat" })).toHaveAttribute("href", "codex://threads/controller-one");
  const dialog = await selectBothAndReview();
  expect(within(dialog).getByRole("list", { name: "Jobs to apply to" })).toHaveTextContent("Orchard · Designer · Source 21");
  expect(posts).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  await waitFor(() => expect(screen.getByText("0 selected")).toBeVisible());
  expect(screen.getByText(/Claude Appliers are filling these forms in your Chrome/)).toBeVisible();
  expect(screen.queryByRole("link", { name: /controller chat/ })).toBeNull();
  expect(posts).toEqual([applied()]);
});

test("an unknown delivery receipt retains the selection and cannot send another request", async () => {
  mockApi({ state: "unknown_delivery" });
  renderQueue();
  const dialog = await selectBothAndReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm and apply" }));
  expect(await screen.findByText(/The request is unconfirmed/)).toBeVisible();
  expect(screen.getByText("2 selected")).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  await waitFor(() => expect(reads.some(url => url.includes(`request_id=${posts[0]!.request_id}`))).toBe(true));
  expect(posts).toHaveLength(1);
});
