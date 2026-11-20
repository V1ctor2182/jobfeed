import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { DashboardOverview } from "@/components/workspace/DashboardOverview";

const NOT_INSTALLED = {
  error: { code: "applications_not_installed", message: "Applying isn't set up on this Jobfeed.", request_id: "r1" },
};

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderOverview(installed: boolean) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/review") return installed ? respond(200, { items: [], sites: [], ended: 0 }) : respond(404, NOT_INSTALLED);
    if (url === "/api/answer-bank") return installed ? respond(200, { entries: [], common_questions: [] }) : respond(404, NOT_INSTALLED);
    if (url.startsWith("/api/runs")) return respond(200, { runs: [] });
    if (url.startsWith("/api/apply-health")) return respond(200, { queue: { state: "missing" } });
    if (url.startsWith("/api/performance")) return respond(200, { total_llm_cost_usd: 0 });
    return respond(200, { totals: { jobs: 0, evaluated: 0, applied: 0 }, daily: [], verdict_distribution: {} });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DashboardOverview />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("without the application pipeline the dashboard shows only finding and ranking", async () => {
  renderOverview(false);
  expect(await screen.findByText("No runs in the last 14 days.")).toBeInTheDocument();
  await vi.waitFor(() => expect(screen.queryByRole("heading", { name: "Application queue" })).not.toBeInTheDocument());
  expect(screen.queryByRole("heading", { name: "Apply progress" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Applied over time" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Recent runs" })).toBeInTheDocument();
});

test("with the application pipeline the application cards stay", async () => {
  renderOverview(true);
  expect(await screen.findByText("No runs in the last 14 days.")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Application queue" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Apply progress" })).toBeInTheDocument();
});
