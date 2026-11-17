import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import JobsPage from "@/routes/jobs";

const now = new Date().toISOString();
const mail = (id: string, company: string, status: string, subject: string, jobIds: number[]) => ({
  id, thread_id: id, sender: "no-reply@example.com", sender_name: null, subject, snippet: `${subject} snippet`, received_at: now, status, company,
  view_url: `https://mail.google.com/#all/${id}`, job_ids: jobIds,
});
const card = (jobId: number | null, company: string, stage: string, extra = {}) => ({
  job_id: jobId, company, title: jobId === null ? null : "Software Engineer", url: jobId === null ? null : `https://example.test/${jobId}`, applied_at: now,
  source: jobId === null ? "email" : "jobfeed", stage, confirmed_by_email: true, email_count: 1, last_activity_at: now, batch_id: jobId === null ? null : "autopilot", ...extra,
});
const state = {
  messages: [mail("m1", "Realm", "rejected", "Realm Application Update", [1]), mail("m2", "Realm", "applied", "Thanks for applying to Realm", [1]), mail("m3", "sandiego", "applied", "USD Application Received", [])],
  applications: [card(1, "Realm", "rejected", { email_count: 2 }), card(2, "Orchard", "applied", { confirmed_by_email: false, email_count: 0, batch_id: null }), card(null, "sandiego", "applied")],
  last_sync: { finished_at: now, new_messages: 3, error: null },
};
const detail = { job: { id: "1", jd_text: "Build the platform." }, status: { status: "applied", notes: "Recruiter is Dana.", history: ["new", "applied"] } };
let posts: { url: string; body: unknown }[];

const queue = {
  state: "available", truncated: true, total: 9, counts_by_state: { Queued: 7, Applying: 1, Cancelled: 1 },
  jobs: [
    { job_id: 10, company: "Harbor", title: "Platform Engineer", queue_state: "Queued", manual_priority: true },
    { job_id: 11, company: "Lumen", title: "Data Engineer", queue_state: "Queued", manual_priority: false },
    { job_id: 12, company: "Gone Co", title: "Engineer", queue_state: "Cancelled", manual_priority: false },
    { job_id: 20, company: "Kestrel", title: "Backend Engineer", queue_state: "Applying", manual_priority: false },
    { job_id: 1, company: "Realm", title: "Software Engineer", queue_state: "Applying", manual_priority: false },
  ],
};
const runs = [
  { job_id: 20, company: "Kestrel", title: "Backend Engineer", state: "parked", updated_at: now, is_live: true },
  { job_id: 21, company: "Mica", title: "ML Engineer", state: "finished", updated_at: now, is_live: false },
  { job_id: 22, company: "Dead Co", title: "Engineer", state: "stopped", updated_at: now, is_live: false },
  { job_id: 1, company: "Realm", title: "Software Engineer", state: "finished", updated_at: now, is_live: false },
];

beforeEach(() => {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") { posts.push({ url, body: JSON.parse(String(init.body)) }); return Response.json({}); }
    if (url === "/api/tracker") return Response.json(state);
    if (url === "/api/apply-health") return Response.json({ queue, batches: [], monitoring: { state: "available" }, runtime: { state: "available" } });
    if (url === "/api/applier-runs") return Response.json({ runs, autopilot_enabled: true });
    if (url.startsWith("/api/jobs/")) return Response.json(detail);
    if (url.startsWith("/api/apply-health/jobs/1/form")) {
      return Response.json({ status: "available", snapshots: [{ id: "capture-1", captured_at: now, fields: [], resume: { filename: "Resume.pdf", label: "Resume-Alex", artifact_url: "/api/apply-health/jobs/1/form/resume?batch_id=autopilot&snapshot_id=capture-1" } }] });
    }
    throw new Error(`unexpected fetch in test: ${url}`);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><JobsPage /></MemoryRouter></QueryClientProvider>);
});
afterEach(() => { vi.unstubAllGlobals(); });

const names = (region: HTMLElement) => within(region).getAllByRole("button").map((button) => button.querySelector("b")?.textContent);

test("the board shows scanned jobs as queued, applying and applied", async () => {
  const queued = await screen.findByRole("region", { name: "Queued: 7" });
  expect(names(queued)).toEqual(["Harbor", "Lumen"]);
  expect(within(queued).getByRole("button", { name: /Harbor/ })).toHaveTextContent("User priority");
  expect(within(queued).getByText("5 more. Search to narrow.")).toBeVisible();
  const applying = screen.getByRole("region", { name: "Applying: 2" });
  expect(names(applying)).toEqual(["Kestrel", "Mica"]);
  expect(within(applying).getByRole("button", { name: /Kestrel/ })).toHaveTextContent("Waiting on you");
  expect(within(applying).getByRole("button", { name: /Mica/ })).toHaveTextContent("Ready for review");
  const applied = screen.getByRole("region", { name: "Applied: 2" });
  expect(names(applied)).toEqual(["Realm", "Orchard"]);
  expect(within(applied).getByRole("button", { name: /Realm/ })).toHaveTextContent("Rejected");
  expect(within(applied).getByRole("button", { name: /Orchard/ })).toHaveTextContent("No confirmation email");
});

test("a job appears in one column only: applied beats applying, and an ended or cancelled job is left out", async () => {
  await screen.findByRole("region", { name: "Queued: 7" });
  expect(screen.getAllByRole("button", { name: /Realm/ })).toHaveLength(1);
  expect(screen.queryByRole("button", { name: /Dead Co|Gone Co|sandiego/ })).toBeNull();
});

test("search narrows every column by company or title", async () => {
  await screen.findByRole("region", { name: "Queued: 7" });
  fireEvent.change(screen.getByRole("searchbox", { name: "Search companies" }), { target: { value: "data eng" } });
  expect(names(screen.getByRole("region", { name: "Queued: 1" }))).toEqual(["Lumen"]);
  expect(screen.getByRole("region", { name: "Applied: 0" })).toBeVisible();
});

test("a job that is not applied yet opens with its status, posting and a link to its Applier trace", async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Kestrel/ }));
  const panel = screen.getByRole("dialog", { name: "Kestrel · Backend Engineer" });
  expect(within(panel).getByText("Waiting on you")).toBeVisible();
  expect(within(panel).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Overview"]);
  expect(within(panel).getByRole("link", { name: /What the Applier is doing/ })).toHaveAttribute("href", "/applier-trace?job=20");
  expect(await within(panel).findByText("Build the platform.")).toBeVisible();
  expect(within(panel).queryByText("Confirmation email")).toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: /Harbor/ }));
  expect(within(screen.getByRole("dialog")).queryByRole("link", { name: /What the Applier is doing/ })).toBeNull();
});

test("a card opens the application with its status, notes, history, latest email and posting", async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Realm/ }));
  const panel = screen.getByRole("dialog", { name: "Realm · Software Engineer" });
  expect(within(panel).getByRole("link", { name: "Job posting ↗" })).toHaveAttribute("href", "https://example.test/1");
  expect(within(panel).getByText("Rejected")).toBeVisible();
  expect(within(panel).getByText("Realm Application Update")).toBeVisible();
  expect(await within(panel).findByText("Build the platform.")).toBeVisible();
  expect(within(panel).getByText("Recruiter is Dana.")).toBeVisible();
  expect(within(panel).getByText("new → applied")).toBeVisible();
  expect(within(panel).getByRole("link", { name: /Applier run and submission evidence/ })).toHaveAttribute("href", "/runs?tab=all&kind=application&run=autopilot&job=1");
  fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("a note is saved to the job", async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Realm/ }));
  fireEvent.change(screen.getByLabelText("Notes"), { target: { value: " Phone screen Friday " } });
  fireEvent.click(screen.getByRole("button", { name: "Add note" }));
  await waitFor(() => expect(posts).toEqual([{ url: "/api/jobs/1/note", body: { text: "Phone screen Friday" } }]));
});

test("the panel shows the résumé that was sent and the linked email", async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Realm/ }));
  const panel = screen.getByRole("dialog");
  fireEvent.click(within(panel).getByRole("tab", { name: "Résumé" }));
  expect(await within(panel).findByTitle("Résumé sent with this application")).toHaveAttribute("src", "/api/apply-health/jobs/1/form/resume?batch_id=autopilot&snapshot_id=capture-1");
  fireEvent.click(within(panel).getByRole("tab", { name: "Emails 2" }));
  expect(within(panel).getAllByRole("listitem").map((item) => item.querySelector("b")?.textContent)).toEqual(["Realm Application Update", "Thanks for applying to Realm"]);
});

test("an applied job with no Applier run says there is no saved form", async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Orchard/ }));
  fireEvent.click(screen.getByRole("tab", { name: "Submission" }));
  expect(screen.getByText(/there is no saved form/)).toBeVisible();
});
