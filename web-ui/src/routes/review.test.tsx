import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import ReviewPage from "@/routes/review";

const question = {
  id: "q1", job_id: 6367, kind: "question", company: "Family Dollar", title: "Software Engineer", field: "Postal Code",
  text: "What postal code goes with the address?", options: ["11101", "11101-2910"], quick_replies: [], created_at: "2026-11-11T06:30:50Z",
};
const drafted = { id: "q2", job_id: 7001, kind: "question", company: "Orchard", title: "Platform engineer", text: "Why Orchard?", draft: "Because of the platform work.", options: [], quick_replies: [], created_at: "2026-11-11T06:31:00Z" };
const blocker = { id: "b1", job_id: 6271, kind: "blocker", blocker_type: "user_only_check", company: "Siemens", title: "Engineer", text: "The site needs a login.", options: [], quick_replies: ["done"], created_at: "2026-11-11T06:35:55Z" };
let posts: { url: string; body: Record<string, unknown> }[];

function mockApi(items: unknown[], sites: unknown[] = []) {
  posts = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body)) });
      return Response.json({ items: [] });
    }
    if (url === "/api/review") return Response.json({ items, sites });
    if (url === "/api/answer-bank") return Response.json({ schema_version: 1, revision: 0, entries: [], common_questions: [] });
    throw new Error(`unexpected fetch in test: ${url}`);
  }));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><ReviewPage /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { vi.unstubAllGlobals(); });

test("one click on an offered option sends it and clears the question", async () => {
  mockApi([question]); renderPage();
  const card = await screen.findByRole("article", { name: "Family Dollar · Software Engineer" });
  fireEvent.click(within(card).getByRole("button", { name: "11101" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]!.url).toBe("/api/review/reply");
  expect(posts[0]!.body).toMatchObject({ request_id: "q1", action: "pick", text: "11101" });
  expect(await screen.findByText("No questions are waiting on you.")).toBeVisible();
});

test("Other opens a box for an answer the options do not cover", async () => {
  mockApi([question]); renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "Other" }));
  expect(screen.getByRole("button", { name: "Send answer" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: " 11109 " } });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  await waitFor(() => expect(posts[0]?.body).toMatchObject({ request_id: "q1", action: "answer", text: "11109" }));
});

test("a drafted answer can be approved as written or sent back with a change request", async () => {
  mockApi([drafted]); renderPage();
  expect(await screen.findByText("Because of the platform work.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
  fireEvent.change(screen.getByLabelText("What should change?"), { target: { value: "Shorter." } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Approve draft" }));
  await waitFor(() => expect(posts[0]?.body).toMatchObject({ request_id: "q2", action: "approve" }));
  expect(posts[0]!.body).not.toHaveProperty("text");
});

test("a job that is only blocked is not listed as a question", async () => {
  mockApi([question, blocker]); renderPage();
  await screen.findByRole("article", { name: "Family Dollar · Software Engineer" });
  expect(screen.queryByRole("article", { name: "Siemens · Engineer" })).toBeNull();
  expect(screen.getByRole("button", { name: "Questions 1" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Blocked jobs 1" })).toBeVisible();
});

test("a blocker the user can answer counts as a question", async () => {
  mockApi([{ ...blocker, id: "b2", blocker_type: "submit_unknown", quick_replies: ["submitted", "not submitted"] }]); renderPage();
  expect(await screen.findByRole("button", { name: "Not submitted" })).toBeVisible();
});

test("a blocked job offers Done, and ignoring it takes a second press", async () => {
  mockApi([blocker]); renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "Blocked jobs 1" }));
  expect(screen.getByText(/These are not questions/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Done" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Ignore job" }));
  expect(posts).toEqual([]);
  expect(screen.getByText(/stops this job's Applier/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Yes, ignore job 6271" }));
  await waitFor(() => expect(posts[0]).toMatchObject({ url: "/api/review/ignore", body: { job_id: 6271 } }));
});

test("jobs stopped by one job board's sign-in become a single action that resumes the board", async () => {
  const stopped = [{ ...blocker, id: "s1", job_id: 5826, company: "Liberty Mutual" }, { ...blocker, id: "s2", job_id: 5916, company: "Stellantis" }];
  const site = { site: "indeed", label: "Sign in to Indeed", held: true, request_ids: ["s1", "s2"], job_ids: [5826, 5916], queued: 145, page_url: "https://secure.indeed.com/auth" };
  mockApi([question, ...stopped, blocker], [site]); renderPage();
  const card = await screen.findByRole("article", { name: "Sign in to Indeed" });
  expect(card).toHaveTextContent("2 jobs stopped at a sign-in · 145 queued Indeed jobs paused");
  expect(card).toHaveTextContent("an Applier never signs in or creates an account for you");
  expect(within(card).getByRole("link", { name: "Sign-in page ↗" })).toHaveAttribute("href", "https://secure.indeed.com/auth");
  expect(screen.getByRole("button", { name: "Questions 2" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Blocked jobs 1" })).toBeVisible();
  expect(screen.queryByRole("article", { name: /Liberty Mutual/ })).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "Show the 2 jobs" }));
  expect(within(card).getByRole("article", { name: /Stellantis/ })).toBeVisible();
  fireEvent.click(within(card).getByRole("button", { name: "I signed in, resume Indeed" }));
  await waitFor(() => expect(posts[0]).toMatchObject({ url: "/api/review/resume-site", body: { site: "indeed" } }));
});

test("old sign-in stops on a board that is not paused are offered as a retry, not as a pause", async () => {
  const stopped = [{ ...blocker, id: "s1", job_id: 5826, company: "Liberty Mutual" }];
  const site = { site: "indeed", label: "Sign in to Indeed", held: false, request_ids: ["s1"], job_ids: [5826], queued: 145, page_url: null, latest_stop_at: "2026-11-10T19:23:58Z" };
  mockApi(stopped, [site]); renderPage();
  const card = await screen.findByRole("article", { name: "Sign in to Indeed" });
  expect(within(card).getByRole("heading", { name: "Retry 1 Indeed job" })).toBeVisible();
  expect(card).toHaveTextContent("Indeed is not paused");
  expect(card).toHaveTextContent("new Indeed jobs are running normally");
  fireEvent.click(within(card).getByRole("button", { name: "I'm signed in, retry them" }));
  await waitFor(() => expect(posts[0]).toMatchObject({ url: "/api/review/resume-site", body: { site: "indeed" } }));
});
