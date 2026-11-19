import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useJobStars, type JobStar } from "@/api/job-stars";
import { StarButton } from "./StarButton";

const calls: { url: string; method: string; body: unknown }[] = [];
let starred = false;
let failRead = false;
let failWrite = false;
let holdRead: Promise<void> | undefined;
let holdWrite: Promise<void> | undefined;

function Harness({ ids = ["1", "2"], submit = () => {} }: { ids?: string[]; submit?: () => void }) {
  const stars = useJobStars(ids);
  return <form onSubmit={event => { event.preventDefault(); submit(); }}>
    {ids.map(id => <StarButton key={id} sourceId={id} label={`Job ${id}`} stars={stars} />)}
  </form>;
}

function mount(props: Parameters<typeof Harness>[0] = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><Harness {...props} /></QueryClientProvider>);
}

beforeEach(() => {
  calls.length = 0;
  starred = false;
  failRead = false;
  failWrite = false;
  holdRead = undefined;
  holdWrite = undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (method === "GET" && url.startsWith("/api/job-stars?")) {
      await holdRead;
      if (failRead) return Response.json({ error: { code: "unavailable", message: "Stars unavailable", request_id: "test" } }, { status: 503 });
      const ids = new URL(url, "http://localhost").searchParams.get("job_ids")!.split(",");
      return Response.json({ stars: ids.map(source_job_id => ({ source_job_id, real_job_id: "shared-canonical", starred })) });
    }
    if (method === "PUT" && /^\/api\/jobs\/\d+\/star$/.test(url)) {
      await holdWrite;
      if (failWrite) return Response.json({ error: { code: "save_failed", message: "Please retry", request_id: "test" } }, { status: 500 });
      starred = JSON.parse(String(init?.body)).starred;
      const result: JobStar = { source_job_id: url.split("/")[3]!, real_job_id: "shared-canonical", starred };
      return Response.json(result);
    }
    throw new Error(`Unexpected request ${method} ${url}`);
  }));
});
afterEach(() => vi.unstubAllGlobals());

it("batches visible source IDs and shows unknown status until the response arrives", async () => {
  let release!: () => void;
  holdRead = new Promise(resolve => { release = resolve; });
  mount();
  expect(screen.queryByRole("button", { name: "Star Job 1" })).toBeNull();
  expect(screen.getByRole("button", { name: "Star status unavailable for Job 1" })).toBeDisabled();
  expect(calls).toHaveLength(1);
  expect(new URL(calls[0]!.url, "http://localhost").searchParams.get("job_ids")).toBe("1,2");
  await act(async () => release());
  expect(await screen.findByRole("button", { name: "Star Job 1" })).toHaveAttribute("aria-pressed", "false");
});

it("chunks large queue reads into batches of at most 100", async () => {
  mount({ ids: Array.from({ length: 205 }, (_, index) => String(index + 1)) });
  await screen.findByRole("button", { name: "Star Job 205" });
  expect(calls).toHaveLength(3);
  expect(calls.map(call => new URL(call.url, "http://localhost").searchParams.get("job_ids")!.split(",").length)).toEqual([100, 100, 5]);
});

it("toggles canonical siblings without submitting forms or starting applications", async () => {
  const submit = vi.fn();
  mount({ submit });
  fireEvent.click(await screen.findByRole("button", { name: "Star Job 1" }));
  expect(await screen.findByRole("button", { name: "Unstar Job 2" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Unstar Job 2" }));
  await screen.findByRole("button", { name: "Star Job 1" });
  expect(submit).not.toHaveBeenCalled();
  expect(calls.filter(call => call.method !== "GET")).toEqual([
    { url: "/api/jobs/1/star", method: "PUT", body: { starred: true } },
    { url: "/api/jobs/2/star", method: "PUT", body: { starred: false } },
  ]);
});

it("disables a pending toggle and keeps the previous star when save fails", async () => {
  starred = true;
  failWrite = true;
  let release!: () => void;
  holdWrite = new Promise(resolve => { release = resolve; });
  mount();
  const button = await screen.findByRole("button", { name: "Unstar Job 1" });
  fireEvent.click(button);
  await waitFor(() => expect(button).toHaveAttribute("aria-disabled", "true"));
  fireEvent.click(button);
  expect(calls.filter(call => call.method === "PUT")).toHaveLength(1);
  expect(button).toHaveAttribute("aria-pressed", "true");
  await act(async () => release());
  expect(await screen.findByRole("alert")).toHaveTextContent("Star not saved. Please retry");
  expect(button).not.toHaveAttribute("aria-disabled", "true");
  expect(button).toHaveAttribute("aria-pressed", "true");
  expect(calls.filter(call => call.method === "PUT")).toHaveLength(1);
});

it("offers an explicit retry when star status could not be read", async () => {
  failRead = true;
  mount();
  const retry = await screen.findByRole("button", { name: "Retry star status for Job 1" });
  expect(screen.queryByRole("button", { name: "Star Job 1" })).toBeNull();
  failRead = false;
  fireEvent.click(retry);
  await screen.findByRole("button", { name: "Star Job 1" });
  expect(calls.every(call => call.method === "GET")).toBe(true);
});
