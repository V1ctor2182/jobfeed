import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApplicationRerunStatus } from "@/api/application-reruns";
import { ApplicationTabJump } from "./ApplicationTabJump";
import { ApplicationJobLinks } from "./ApplicationJobLinks";

let status: string;
let csrf: string;
let error: string | null;
let requests: { url: string; init?: RequestInit }[];
let rerunStatus: ApplicationRerunStatus;

beforeEach(() => {
  status = "focused";
  csrf = "csrf-current";
  error = null;
  requests = [];
  rerunStatus = { csrf_token: "csrf-current", eligible: true, reason: null, owner_thread_id: "owner-42", request: null };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url.startsWith("/api/application-reruns?")) return Response.json(rerunStatus);
    if (url.endsWith("/open-existing")) {
      if (error) throw new Error(error);
      return Response.json({ status });
    }
    return Response.json({ csrf_token: csrf, bindings: [] });
  }));
});
afterEach(() => vi.unstubAllGlobals());

function mount(owner: string | null = "owner-42", duplicate = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <ApplicationTabJump jobId={42} owner={owner} />
    {duplicate && <ApplicationTabJump jobId={43} owner="owner-43" />}
  </QueryClientProvider>);
}

async function clickOpen() {
  const button = await screen.findByRole("button", { name: "Open application form" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

it("shares read-only discovery and opens the recorded form in one click without manual pairing", async () => {
  mount("owner-42", true);
  const buttons = await screen.findAllByRole("button", { name: "Open application form" });
  await waitFor(() => expect(buttons[0]).toBeEnabled());
  expect(requests).toHaveLength(1);
  expect(requests[0]!.url).toBe("/api/application-tabs");
  expect(requests.filter(r => r.init?.method === "POST")).toEqual([]);
  fireEvent.click(buttons[0]!);
  expect(await screen.findByText("Application form opened in Chrome.")).toBeVisible();
  const writes = requests.filter(r => r.init?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0]!.url).toBe("/api/application-tabs/42/open-existing");
  expect(JSON.parse(String(writes[0]!.init?.body))).toEqual({ owner_thread_id: "owner-42" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("refreshes the server token before opening after a service restart", async () => {
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "Open application form" })).toBeEnabled());
  csrf = "csrf-after-restart";
  await clickOpen();
  expect(await screen.findByText("Application form opened in Chrome.")).toBeVisible();
  const open = requests.find(r => r.url.endsWith("/open-existing"))!;
  expect(open.init?.headers).toMatchObject({ "X-Application-Tabs-CSRF": "csrf-after-restart" });
});

it.each([
  ["tab_closed", "The application form tab is closed. Open the applier chat to recover it."],
  ["page_changed", "The recorded Chrome tab has changed page. Open the applier chat to check it."],
  ["focus_failed", "Chrome could not bring the application form forward. Try again."],
])("reports %s without claiming success or opening another page", async (result, message) => {
  status = result;
  mount();
  await clickOpen();
  expect(await screen.findByText(message)).toBeVisible();
  expect(screen.queryByText("Application form opened in Chrome.")).not.toBeInTheDocument();
  expect(requests.filter(r => r.init?.method === "POST")).toHaveLength(1);
});

it("does not retry an uncertain open or leave an old success visible", async () => {
  mount();
  await clickOpen();
  await screen.findByText("Application form opened in Chrome.");
  error = "Request timed out";
  await clickOpen();
  expect(await screen.findByText(/Could not confirm the jump. Check Chrome before retrying./)).toBeVisible();
  expect(screen.queryByText("Application form opened in Chrome.")).not.toBeInTheDocument();
  expect(requests.filter(r => r.init?.method === "POST")).toHaveLength(2);
});

it("disables opening when there is no recorded owner", () => {
  mount(null);
  expect(screen.getByRole("button", { name: "Open application form" })).toBeDisabled();
  expect(screen.getByText("Applier unavailable.")).toBeVisible();
  expect(requests).toHaveLength(0);
});

function mountJob(submitted = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ApplicationJobLinks batchId="batch-42" job={{
    job_id: 42, title: "Engineer", company: "Example Co", owner_thread_id: "owner-42", job_url: "https://example.com/jobs/42", counted: true,
    closed: false, paused: false, submitted, synced: submitted, terminal: false, user_submitted: false,
  }} /></QueryClientProvider>);
}

it("keeps posting and chat links distinct from the actual form", async () => {
  mountJob();
  expect(screen.getByRole("link", { name: /Open job posting/ })).toHaveAttribute("href", "https://example.com/jobs/42");
  expect(screen.getByRole("link", { name: "Open applier chat" })).toHaveAttribute("href", "codex://threads/owner-42");
  expect(screen.getByRole("button", { name: "Open application form" })).not.toHaveAttribute("href");
  await clickOpen();
  expect(await screen.findByText("Application form opened in Chrome.")).toBeVisible();
});

it("keeps the preserved form accessible after submission", async () => {
  mountJob(true);
  await clickOpen();
  expect(await screen.findByText("Application form opened in Chrome.")).toBeVisible();
  expect(screen.getByRole("button", { name: "View saved confirmation" })).toBeVisible();
});

it("opens the replacement owner's form even when the saved observation is old", async () => {
  rerunStatus.owner_thread_id = "new-applier";
  mountJob();
  await waitFor(() => expect(screen.getByRole("link", { name: "Open applier chat" })).toHaveAttribute("href", "codex://threads/new-applier"));
  await clickOpen();
  await screen.findByText("Application form opened in Chrome.");
  const open = requests.find(r => r.url.endsWith("/open-existing"))!;
  expect(JSON.parse(String(open.init?.body))).toEqual({ owner_thread_id: "new-applier" });
});

it("hides the old form action while an explicit rerun replaces it", async () => {
  rerunStatus = { ...rerunStatus, eligible: false, request: { request_id: "restart", batch_id: "batch-42", job_id: 42, owner_thread_id: "owner-42", state: "running", phase: "cleanup", reason: null, created_at: "2026-10-22T20:00:00Z", turn_id: "restart-turn" } };
  mountJob();
  expect(await screen.findByText("The new application form will be available after the applier is connected.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Open application form" })).not.toBeInTheDocument();
  expect(requests.filter(r => r.init?.method === "POST")).toHaveLength(0);
});

it("never shows an earlier job's open result after the selected job changes", async () => {
  let resolveOpen: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).endsWith("/open-existing")
    ? new Promise<Response>(resolve => { resolveOpen = resolve; })
    : Promise.resolve(Response.json({ csrf_token: "current", bindings: [] }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (jobId: number) => <QueryClientProvider client={client}><ApplicationTabJump jobId={jobId} owner={`owner-${jobId}`} /></QueryClientProvider>;
  const view = render(content(42));
  await clickOpen();
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/application-tabs/42/open-existing", expect.anything()));
  view.rerender(content(43));
  await act(async () => { resolveOpen(Response.json({ status: "focused" })); });
  expect(screen.queryByText("Application form opened in Chrome.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open application form" })).toBeEnabled();
});
