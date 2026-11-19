import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import TrackerPage from "@/routes/tracker";

const now = new Date().toISOString();
const mail = (id: string, company: string, status: string, subject: string) => ({
  id, thread_id: id, sender: "no-reply@example.com", sender_name: null, subject, snippet: "snippet", received_at: now, status, company,
  view_url: `https://mail.google.com/#all/${id}`, job_ids: [],
});
const card = (company: string, stage: string, extra = {}) => ({
  job_id: 1, company, title: "Software Engineer", url: null, applied_at: now, source: "jobfeed", stage, confirmed_by_email: true, email_count: 1, last_activity_at: now, ...extra,
});
const state = {
  messages: [mail("m1", "Realm", "rejected", "Realm Application Update"), mail("m2", "Orchard", "applied", "Thanks for applying"), mail("m3", "Harbor", "interviewing", "Interview with Harbor")],
  applications: [card("Realm", "rejected"), card("Orchard", "applied", { confirmed_by_email: false, email_count: 0 }), card("Harbor", "interviewing"), card("sandiego", "applied", { job_id: null, title: null, source: "email" })],
  last_sync: { finished_at: now, new_messages: 3, error: null },
};

const bodies: string[] = [];

function renderPage(path = "/tracker", data: unknown = state, bodyStatus = 200) {
  bodies.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/tracker/messages/")) {
      bodies.push(url);
      if (bodyStatus !== 200) return Response.json({ error: { code: "tracker_unavailable", message: "down" } }, { status: bodyStatus });
      return Response.json({ ...state.messages[0], body: "Dear Alex,\n\nWe decided to move forward with other candidates.", recipients: ["me@example.com"], fetched_at: now });
    }
    return Response.json(data);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><TrackerPage /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { vi.unstubAllGlobals(); });

test("the inbox lists application email and says what each reports", async () => {
  renderPage();
  const list = await screen.findByRole("list", { name: "Application email" });
  expect(within(list).getAllByRole("button").map((item) => item.textContent)).toEqual([
    expect.stringContaining("Rejected: Realm Application Update"), expect.stringContaining("Received: Thanks for applying"), expect.stringContaining("Interview: Interview with Harbor"),
  ]);
  expect(within(screen.getByRole("region", { name: "Message" })).getByText("Select a message to read")).toBeVisible();
});

test("opening a message shows its body beside the list and fetches it once", async () => {
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Realm Application Update/ }));
  const reader = screen.getByRole("region", { name: "Message" });
  expect(within(reader).getByRole("heading", { name: "Realm Application Update" })).toBeVisible();
  expect(within(reader).getByRole("status")).toHaveTextContent("Fetching this email from Gmail");
  expect(await within(reader).findByText(/We decided to move forward with other candidates/)).toBeVisible();
  expect(within(reader).getByText("to me@example.com")).toBeVisible();
  expect(within(reader).getByRole("link", { name: "Open in Gmail ↗" })).toHaveAttribute("href", "https://mail.google.com/#all/m1");
  expect(screen.getByRole("button", { name: /Realm Application Update/ })).toHaveAttribute("aria-current", "true");
  fireEvent.click(screen.getByRole("button", { name: /Thanks for applying/ }));
  fireEvent.click(screen.getByRole("button", { name: /Realm Application Update/ }));
  expect(bodies.filter((url) => url.endsWith("/m1"))).toHaveLength(1);
});

test("a body that cannot be fetched falls back to the snippet", async () => {
  renderPage("/tracker", state, 503);
  fireEvent.click(await screen.findByRole("button", { name: /Realm Application Update/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This email could not be fetched. The snippet is: snippet");
});

test("the inbox filter and search narrow the list", async () => {
  renderPage();
  await screen.findByRole("list", { name: "Application email" });
  fireEvent.click(screen.getByRole("button", { name: "Rejected 1" }));
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "All 3" }));
  fireEvent.change(screen.getByRole("searchbox", { name: "Search messages" }), { target: { value: "harbor" } });
  expect(screen.getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "All 1" })).toBeVisible();
});

test("a failed mail check is called out", async () => {
  renderPage("/tracker", { ...state, last_sync: { finished_at: now, new_messages: 0, error: "GmailError: down" } });
  expect(await screen.findByText(/The last check failed/)).toBeVisible();
});

test("the Pipeline tab shows each application in the stage its email puts it in", async () => {
  renderPage("/tracker?tab=pipeline");
  const applied = await screen.findByRole("region", { name: "Applied: 2" });
  expect(within(applied).getByRole("button", { name: /Orchard/ })).toHaveTextContent("No confirmation email");
  expect(within(applied).getByRole("button", { name: /sandiego/ })).toHaveTextContent("Found in email only");
  expect(within(screen.getByRole("region", { name: "Rejected: 1" })).getByRole("button", { name: /Realm/ })).toBeVisible();
  expect(within(screen.getByRole("region", { name: "Offer: 0" })).getByText("No jobs")).toBeVisible();
  expect(screen.getByText("2 of 3 recorded applications have a confirmation email.")).toBeVisible();
});

test("a Pipeline card opens the application, and one found only in email has no form tabs", async () => {
  renderPage("/tracker?tab=pipeline");
  fireEvent.click(await screen.findByRole("button", { name: /sandiego/ }));
  expect(within(screen.getByRole("dialog")).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Overview", "Emails 0"]);
});

test("switching to Pipeline keeps the choice in the address and searches companies", async () => {
  renderPage();
  await screen.findByRole("list", { name: "Application email" });
  fireEvent.click(screen.getByRole("tab", { name: "Pipeline" }));
  expect(await screen.findByRole("region", { name: "Rejected: 1" })).toBeVisible();
  fireEvent.change(screen.getByRole("searchbox", { name: "Search companies" }), { target: { value: "orch" } });
  expect(screen.getByRole("region", { name: "Rejected: 0" })).toBeVisible();
});
