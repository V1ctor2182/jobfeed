import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { applicationRerunKey, type ApplicationRerunStatus } from "@/api/application-reruns";
import type { ObservedJob } from "@/api/workspace";
import { ApplicationRerunButton } from "./ApplicationRerunButton";

const job: ObservedJob = { job_id: 42, title: "Software Engineer", company: "Rilla", owner_thread_id: "owner-42", counted: true, closed: false, paused: false, submitted: false, synced: false, terminal: false, user_submitted: false };
let status: ApplicationRerunStatus;
let posts: RequestInit[];
let failPost: boolean;
let releasePost: (() => void) | undefined;
let deferPost: boolean;

beforeEach(() => {
  sessionStorage.clear();
  posts = [];
  failPost = false;
  deferPost = false;
  releasePost = undefined;
  status = { csrf_token: "csrf-current", eligible: true, reason: null, owner_thread_id: "owner-42", request: null };
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push(init);
      if (deferPost) await new Promise<void>(resolve => { releasePost = resolve; });
      if (failPost) throw new Error("Connection lost");
      const body = JSON.parse(String(init.body));
      status = { ...status, eligible: false, request: { request_id: body.request_id, batch_id: body.batch_id, job_id: body.job_id, owner_thread_id: body.expected_owner, state: "queued", reason: null, created_at: "2026-10-22T20:00:00Z", turn_id: null } };
    }
    return Response.json(status);
  }));
});
afterEach(() => vi.unstubAllGlobals());

function mount(duplicate = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}>
    <ApplicationRerunButton batchId="rilla-observation" job={job} />
    {duplicate && <ApplicationRerunButton batchId="rilla-observation" job={job} />}
  </QueryClientProvider>) };
}
async function open() {
  await waitFor(() => expect(screen.getAllByRole("button", { name: "Rerun application" })[0]).toBeEnabled());
  fireEvent.click(screen.getAllByRole("button", { name: "Rerun application" })[0]!);
  return screen.findByRole("dialog");
}

it("requires confirmation for the exact job and restores focus on cancel without rerunning", async () => {
  mount();
  const dialog = await open();
  expect(within(dialog).getByText("Software Engineer")).toBeVisible();
  expect(within(dialog).getByText("Rilla")).toBeVisible();
  expect(within(dialog).getByText(/The batch stays stopped/)).toBeVisible();
  expect(within(dialog).getByText(/Close the previous application tab and archive its applier chat/)).toBeVisible();
  expect(within(dialog).getByText(/same resume and approved answers/)).toBeVisible();
  expect(posts).toEqual([]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(screen.getByRole("button", { name: "Rerun application" })).toHaveFocus());
  expect(posts).toEqual([]);
});

it("sends one scoped request and shares queued state between row and detail", async () => {
  deferPost = true;
  mount(true);
  const dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Rerun application" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  for (const button of screen.getAllByRole("button", { name: /Rerun application|Requesting application rerun/ })) expect(button).toBeDisabled();
  expect(JSON.parse(String(posts[0]!.body))).toEqual({ request_id: expect.stringMatching(/^[a-f0-9-]{36}$/), batch_id: "rilla-observation", job_id: 42, expected_owner: "owner-42" });
  expect(posts[0]!.headers).toMatchObject({ "X-Application-Reruns-CSRF": "csrf-current" });
  await act(async () => releasePost!());
  expect(await screen.findAllByText("Rerun queued. Waiting for the applier to start.")).toHaveLength(2);
  expect(screen.queryByText("Applier is running.")).not.toBeInTheDocument();
  expect(posts).toHaveLength(1);
});

it.each([null, "completed", "blocked"] as const)("persists an ambiguous request and retries the same UUID after a fresh eligible read with previous state %s", async previousState => {
  if (previousState) status = { ...status, request: { request_id: "older-request", batch_id: "rilla-observation", job_id: 42, owner_thread_id: "owner-42", state: previousState, reason: null, created_at: "2026-10-21T20:00:00Z", turn_id: "older-turn" } };
  failPost = true;
  const first = mount();
  fireEvent.click(within(await open()).getByRole("button", { name: "Rerun application" }));
  expect(await screen.findByText(/Rerun delivery is unconfirmed. Checking/)).toBeVisible();
  const requestId = JSON.parse(String(posts[0]!.body)).request_id;
  expect(sessionStorage.getItem('["application-reruns","rilla-observation",42,"attempt"]')).toContain(requestId);
  first.unmount();
  mount();
  expect(await screen.findByText(/Rerun delivery is unconfirmed. Checking/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Rerun application" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check rerun status" }));
  expect(posts).toHaveLength(1);
  failPost = false;
  fireEvent.click(await screen.findByRole("button", { name: "Retry rerun request" }));
  expect(await screen.findByText("Rerun queued. Waiting for the applier to start.")).toBeVisible();
  expect(posts).toHaveLength(2);
  expect(JSON.parse(String(posts[1]!.body)).request_id).toBe(requestId);
});

it.each([null, "The applier reported a terminal or uncertain outcome. Verify it first.", "The previous rerun could not finish."])("explains unavailable reruns without duplicating their reason %s", async requestReason => {
  status = { ...status, eligible: false, reason: "The applier reported a terminal or uncertain outcome. Verify it first.", request: requestReason ? { request_id: "previous", batch_id: "rilla-observation", job_id: 42, owner_thread_id: "owner-42", state: "blocked", reason: requestReason, created_at: "2026-10-22T20:00:00Z", turn_id: "previous-turn" } : null };
  mount();
  expect(await screen.findByText("Rerun unavailable. The applier reported a terminal or uncertain outcome. Verify it first.")).toBeVisible();
  expect(screen.getAllByText(/The applier reported a terminal or uncertain outcome/)).toHaveLength(1);
  if (requestReason === "The previous rerun could not finish.") expect(screen.getByText("Rerun needs attention. The previous rerun could not finish.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Rerun application" })).toBeDisabled();
  expect(posts).toEqual([]);
});

it("keeps an unknown server delivery blocked even if eligibility is stale", async () => {
  status = { ...status, request: { request_id: "previous", batch_id: "rilla-observation", job_id: 42, owner_thread_id: "owner-42", state: "unknown", reason: null, created_at: "2026-10-22T20:00:00Z", turn_id: null } };
  mount();
  expect(await screen.findByText(/Rerun delivery is unconfirmed. Check status before/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Rerun application" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Check rerun status" }));
  expect(posts).toEqual([]);
});

it("uses the authoritative current owner when saved observations still show the old applier", async () => {
  status = { ...status, owner_thread_id: "different-owner" };
  mount();
  fireEvent.click(within(await open()).getByRole("button", { name: "Rerun application" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(JSON.parse(String(posts[0]!.body)).expected_owner).toBe("different-owner");
});

it("requires a new confirmation if ownership changes while the dialog is open", async () => {
  const { client } = mount();
  const dialog = await open();
  await act(async () => client.setQueryData(applicationRerunKey("rilla-observation", 42), { ...status, owner_thread_id: "replacement-owner" }));
  expect(await screen.findByText("Applier changed. Close this confirmation and try again.")).toBeVisible();
  expect(within(dialog).getByRole("button", { name: "Rerun application" })).toBeDisabled();
  expect(posts).toEqual([]);
});

it.each([
  ["relay_pending", "Restart queued. Waiting to replace the previous form and applier."],
  ["cleanup", "Closing the previous application tab."],
  ["create_unknown", "Checking whether the new applier was created. No duplicate will be started."],
  ["candidate_received", "New applier created. Connecting it to this application."],
  ["adopting", "Connecting the new applier to this application."],
  ["adopted", "New applier connected. Waiting for form filling to start."],
  ["filling", "The new applier is running with instructions to fill the form and stop before Submit."],
] as const)("shows restart progress for %s without sending another command", async (phase, message) => {
  status = { ...status, eligible: false, request: { request_id: "restart", batch_id: "rilla-observation", job_id: 42, owner_thread_id: "owner-42", state: phase === "create_unknown" ? "unknown" : "running", phase, reason: null, created_at: "2026-10-22T20:00:00Z", turn_id: "restart-turn" } };
  mount();
  expect(await screen.findByText(message)).toBeVisible();
  expect(screen.getByRole("button", { name: "Rerun application" })).toBeDisabled();
  expect(posts).toEqual([]);
});

it("does not display an old cleanup phase as active after the restart is blocked", async () => {
  status = { ...status, eligible: false, reason: "The stopped scope changed.", request: { request_id: "restart", batch_id: "rilla-observation", job_id: 42, owner_thread_id: "owner-42", state: "blocked", phase: "cleanup", reason: "The stopped scope changed.", created_at: "2026-10-22T20:00:00Z", turn_id: "restart-turn" } };
  mount();
  expect(await screen.findByText("Restart needs attention.")).toBeVisible();
  expect(screen.queryByText("Closing the previous application tab.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Rerun application" })).toBeDisabled();
});
