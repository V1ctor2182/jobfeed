import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import type { ObservedJob, QueueJob } from "@/api/workspace";
import { ApplicationTable, needsAttention } from "./ApplicationTable";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn().mockResolvedValue({ status: "unverified" }) }));

const jobs: QueueJob[] = [
  { job_id: 21, title: "Chosen designer role", company: "Orchard", queue_state: "Queued", retry_count: 0, manual_priority: true },
  { job_id: 11, title: "Discovered engineer role", company: "Harbor", queue_state: "Queued", retry_count: 0, manual_priority: false },
];

test("shows user priority in queue order and filters it independently of queue state", async () => {
  renderTable(jobs);
  const table = screen.getByRole("table", { name: /Application queue/ });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("Chosen designer role");
  expect(rows[0]).toHaveTextContent("User priority");
  expect(rows[1]).toHaveTextContent("Discovered engineer role");
  expect(rows[1]).toHaveTextContent("Standard");

  fireEvent.mouseDown(screen.getByRole("button", { name: /Priority/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "User priority" }));
  expect(within(table).getByRole("button", { name: "Chosen designer role" })).toBeVisible();
  expect(within(table).queryByRole("button", { name: "Discovered engineer role" })).not.toBeInTheDocument();
  fireEvent.click(within(table).getByRole("button", { name: "Chosen designer role" }));
  const details = document.getElementById("queue-job-details-21")!;
  expect(within(details).getByText("Priority").nextElementSibling).toHaveTextContent("User priority");
  expect(within(details).getByText("Queue state").nextElementSibling).toHaveTextContent("Queued");
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));

  fireEvent.mouseDown(screen.getByRole("button", { name: /Priority/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "Standard" }));
  expect(within(table).getByRole("button", { name: "Discovered engineer role" })).toBeVisible();
  expect(within(table).queryByRole("button", { name: "Chosen designer role" })).not.toBeInTheDocument();
});

test("labels and filters jobs set aside as CAPTCHA needed, which cannot be selected to apply", async () => {
  const hold = { kind: "captcha" as const, detail: "Cloudflare asks to verify you are human.", page_url: "https://jobs.example.test/apply/31", created_at: "2026-11-04T09:30:00+00:00" };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ApplicationTable enableHandoff jobs={[
    { job_id: 31, title: "Held analyst role", company: "Quarry", queue_state: "Queued", retry_count: 0, manual_priority: false, hold },
    ...jobs,
  ]} /></QueryClientProvider>);
  const table = screen.getByRole("table", { name: /Application queue/ });
  expect(within(table).getAllByRole("row")[1]).toHaveTextContent("CAPTCHA needed");
  expect(screen.getByRole("checkbox", { name: "Select queued Quarry Held analyst role" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "Select queued Orchard Chosen designer role" })).toBeEnabled();

  fireEvent.mouseDown(screen.getByRole("button", { name: /Hold/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "CAPTCHA needed" }));
  expect(within(table).getAllByRole("row").slice(1)).toHaveLength(1);
  fireEvent.click(within(table).getByRole("button", { name: "Held analyst role" }));
  const details = document.getElementById("queue-job-details-31")!;
  expect(within(details).getByText("Hold").nextElementSibling).toHaveTextContent("CAPTCHA needed since");
  expect(within(details).getByRole("link", { name: /Open the page/ })).toHaveAttribute("href", hold.page_url);
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));

  fireEvent.mouseDown(screen.getByRole("button", { name: /Hold/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: "Not held" }));
  expect(within(table).getAllByRole("row").slice(1)).toHaveLength(2);
  expect(within(table).queryByRole("button", { name: "Held analyst role" })).not.toBeInTheDocument();
});

test("explains manual priority and authorization even when the real queue is empty", () => {
  renderTable([]);
  expect(screen.getByText("No application jobs are queued.")).toBeVisible();
  expect(screen.getByText(/Jobs you add have user priority and appear first/)).toHaveTextContent("Adding jobs does not start applications. Select queued jobs and click Apply when you are ready.");
  expect(screen.queryByRole("button", { name: /Start|Apply|Submit/ })).not.toBeInTheDocument();
});


function renderTable(rows: QueueJob[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ApplicationTable jobs={rows} /></QueryClientProvider>);
}

test("star changes leave queue priority and detail selection untouched", async () => {
  let starred = true;
  vi.mocked(apiFetch).mockImplementation(async (path, init) => {
    if (path.startsWith("/api/job-stars?")) return { stars: [
      { source_job_id: "21", real_job_id: "chosen", starred },
      { source_job_id: "11", real_job_id: "discovered", starred: false },
    ] };
    if (path === "/api/jobs/21/star") {
      starred = JSON.parse(String(init?.body)).starred;
      return { source_job_id: "21", real_job_id: "chosen", starred };
    }
    return { status: "unverified" };
  });
  renderTable(jobs);
  fireEvent.click(await screen.findByRole("button", { name: "Unstar Orchard Chosen designer role" }));
  await screen.findByRole("button", { name: "Star Orchard Chosen designer role" });
  expect(document.getElementById("queue-job-details-21")).toBeNull();
  const rows = within(screen.getByRole("table", { name: /Application queue/ })).getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("User priority");
  expect(rows[0]).toHaveTextContent("Queued");
  expect(rows[1]).toHaveTextContent("Standard");
  expect(jobs[0]!.manual_priority).toBe(true);
  expect(vi.mocked(apiFetch).mock.calls.filter(([, init]) => init?.method === "PUT").map(([path]) => path)).toEqual(["/api/jobs/21/star"]);
});

test("only a live blocker, error or waiting state needs attention", () => {
  const job = (fields: Partial<ObservedJob>) => ({ job_id: 1, queue_state: "Applying", paused: false, blocked: null, errors: [], terminal: false, ...fields }) as ObservedJob;
  expect(needsAttention(job({ blocked: "needs_input" }))).toBe(true);
  expect(needsAttention(job({ queue_state: "Needs Input" }))).toBe(true);
  expect(needsAttention(job({ queue_state: "Cancelled", paused: true, blocked: "needs_input" }))).toBe(false);
  expect(needsAttention(job({ queue_state: "Unavailable", terminal: true }))).toBe(false);
  expect(needsAttention(job({ queue_state: "Queued", paused: true }))).toBe(false);
});
