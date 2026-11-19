import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import SetupChecklistPage from "./setup-checklist";

const snapshot = { sections: [
  { id: "intake", title: "URL import", checks: [{ id: "mcp", label: "Jobfeed project connection", status: "ready", detail: "Registration uses this database." }] },
  { id: "discovery", title: "Discovery and scoring", checks: [{ id: "resume", label: "Scoring résumé", status: "needs_action", detail: "Your confirmed résumé has not been applied.", action_label: "Review and save", action_href: "/setup/review" }] },
  { id: "applications", title: "Applications", checks: [{ id: "browser", label: "Live browser check", status: "not_tested", detail: "No current diagnostic result." }] },
] };

function Location() { return <div data-testid="location">{useLocation().pathname}</div>; }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/setup"]}><SetupChecklistPage /><Location /></MemoryRouter></QueryClientProvider>);
}

it("keeps independent capability states and starts no work on page load", async () => {
  const requests: { path: string; method: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input); requests.push({ path, method: init?.method ?? "GET" });
    return new Response(JSON.stringify(path.endsWith("checklist") ? snapshot : { csrf_token: "test", run: null }));
  }));
  mount();
  expect(await screen.findByRole("heading", { name: /URL import/ })).toBeVisible();
  expect(screen.getByText("Needs attention")).toBeVisible();
  expect(screen.getByText("Not verified")).toBeVisible();
  expect(screen.getByText("Your confirmed résumé has not been applied.")).toBeVisible();
  expect(screen.getByRole("heading", { name: "Add your first URLs with Codex or Claude Code" })).toBeVisible();
  expect(screen.getByText(/Open a local Codex or Claude Code chat/)).toHaveTextContent("check Jobfeed capabilities");
  expect(screen.getByText(/Use the jobfeed MCP to add these job URLs/)).toHaveTextContent("as my priorities");
  expect(screen.getByText(/Use exact job URLs/)).toHaveTextContent("Job or company names need to be resolved to job URLs first.");
  expect(requests.every(request => request.method === "GET")).toBe(true);
  expect(requests.some(request => /scan|watch|enqueue|start-batch/.test(request.path))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Review and save" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/setup/review"));
});

it("does not claim readiness when the checklist endpoint is unavailable", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).endsWith("checklist") ? {} : { csrf_token: "test", run: null }), { status: String(input).endsWith("checklist") ? 503 : 200 })));
  mount();
  expect(await screen.findByText("Setup status could not be loaded")).toBeVisible();
  expect(screen.queryByText("Ready")).not.toBeInTheDocument();
});

it("makes profile editing a visible onboarding action", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).endsWith("checklist") ? snapshot : { csrf_token: "test", run: null }))));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "My Profile" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/profile"));
});
