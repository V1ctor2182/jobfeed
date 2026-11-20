import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { Sidebar } from "@/components/shell/Sidebar";

const NOT_INSTALLED = {
  error: { code: "applications_not_installed", message: "Applying isn't set up on this Jobfeed.", request_id: "r1" },
};

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderSidebar(installed: boolean) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/review") return installed ? respond(200, { items: [], sites: [], ended: 0 }) : respond(404, NOT_INSTALLED);
    if (url === "/api/answer-bank") return installed ? respond(200, { entries: [], common_questions: [] }) : respond(404, NOT_INSTALLED);
    return respond(200, { total: 0 });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/sources"]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a Jobfeed without the application pipeline hides the application pages", async () => {
  renderSidebar(false);
  await waitFor(() => expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument());
  for (const name of ["Tracker", "Answer Bank", "Applier trace", "Apply health"]) {
    expect(screen.queryByRole("link", { name })).not.toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "Jobs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Profile" })).toBeInTheDocument();
});

test("a Jobfeed with the application pipeline keeps them", async () => {
  renderSidebar(true);
  await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/review", expect.anything()));
  for (const name of ["Review", "Tracker", "Answer Bank", "Applier trace", "Apply health"]) {
    expect(screen.getByRole("link", { name })).toBeInTheDocument();
  }
});
