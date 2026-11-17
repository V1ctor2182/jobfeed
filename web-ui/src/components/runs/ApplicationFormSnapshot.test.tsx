import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { apiFetch } from "@/api/client";
import type { ApplicationForm, FormField, FormSnapshot } from "@/api/application-forms";
import { ApplicationFormSnapshot } from "./ApplicationFormSnapshot";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

function field(overrides: Partial<FormField> = {}): FormField {
  return { id: "email", question: "Email", value: "applicant@example.test", state: "observed", written_value: null, page_url: null, section: null, ...overrides };
}

function snapshot(overrides: Partial<FormSnapshot> = {}): FormSnapshot {
  return {
    id: "capture-2", captured_at: "2026-10-23T14:30:00Z", owner_thread_id: "owner-new", attempt_id: "attempt-2",
    provenance: "captured", coverage: "visible_controls", source_url: "https://jobs.example.test/application",
    fields: [field()], resume: { filename: "selected-resume.pdf", label: "Software engineer resume",
      artifact_url: "/api/apply-health/jobs/42/form/resume?batch_id=batch-a&snapshot_id=capture-2", upload_status: "upload_pending" },
    ...overrides,
  };
}

function response(snapshots: FormSnapshot[] = [snapshot()]): ApplicationForm {
  return { job_id: 42, status: "available", reason: null, snapshots };
}

function renderForm() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(<QueryClientProvider client={client}><ApplicationFormSnapshot batchId="batch-a" jobId={42} /></QueryClientProvider>);
  return { ...result, client };
}

beforeEach(() => { vi.mocked(apiFetch).mockReset(); });

test("shows readable captured values, repeated questions, empty values and the exact saved resume", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response([snapshot({ fields: [
    field(), field({ id: "why-1", question: "Why this role?", value: "First paragraph.\n\nSecond paragraph." }),
    field({ id: "why-2", question: "Why this role?", value: "A different section answer", section: "Additional questions" }),
    field({ id: "sponsorship", question: "Need sponsorship?", value: false }),
    field({ id: "years", question: "Years of experience", value: 0 }),
    field({ id: "middle-name", question: "Middle name", value: "", state: "unanswered" }),
    field({ id: "choices", question: "Skills", value: ["Python", "SQL"], written_value: ["Python", "SQL"] }),
  ] })]));
  renderForm();
  expect(await screen.findByText("applicant@example.test")).toBeVisible();
  expect(screen.getAllByText("Why this role?")).toHaveLength(2);
  expect(screen.getByText(/First paragraph/).textContent).toBe("First paragraph.\n\nSecond paragraph.");
  expect(screen.getByText("No")).toBeVisible();
  expect(screen.getByText("0")).toBeVisible();
  expect(screen.getByText("Empty")).toBeVisible();
  expect(screen.getByText("Unanswered")).toBeVisible();
  expect(screen.getByText(/Python/).textContent).toBe("Python\nSQL");
  expect(screen.queryByText("Previously written")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open saved resume" })).toHaveAttribute("href", "/api/apply-health/jobs/42/form/resume?batch_id=batch-a&snapshot_id=capture-2");
  expect(screen.getByRole("link", { name: "Download resume" })).toHaveAttribute("download", "selected-resume.pdf");
  expect(screen.getByText("Upload selected. Completion not verified.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Recorded form page" })).toHaveAttribute("href", "https://jobs.example.test/application");
});

test("defaults to latest capture and lets the user inspect a previous form and resume", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response([snapshot(), snapshot({ id: "capture-1", captured_at: "2026-10-22T14:30:00Z", attempt_id: "attempt-1",
    fields: [field({ value: "previous@example.test" })], resume: { filename: "earlier.pdf", label: null, artifact_url: "/api/apply-health/jobs/42/form/resume?batch_id=batch-a&snapshot_id=capture-1", upload_status: "uploaded" },
  })]));
  renderForm();
  expect(await screen.findByText("applicant@example.test")).toBeVisible();
  fireEvent.mouseDown(screen.getByRole("button", { name: /Saved capture/ }));
  fireEvent.mouseUp(await screen.findByRole("option", { name: /Earlier capture/ }));
  expect(screen.getByText("previous@example.test")).toBeVisible();
  expect(screen.queryByText("applicant@example.test")).not.toBeInTheDocument();
  expect(screen.getByText("earlier.pdf")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open saved resume" })).toHaveAttribute("href", "/api/apply-health/jobs/42/form/resume?batch_id=batch-a&snapshot_id=capture-1");
});

test("labels legacy records as partial and separates unverified writes from observed values", async () => {
  vi.mocked(apiFetch).mockResolvedValue({ ...response([snapshot({ provenance: "legacy", coverage: "partial_history", owner_thread_id: null, attempt_id: null, resume: null,
    fields: [field({ question: "Phone", state: "written_unverified", value: null, written_value: "555-0100" }), field({ id: "changed", question: "City", value: "Boston", written_value: "New York" })],
  })]), status: "partial", reason: "Older records have incomplete coverage." });
  renderForm();
  expect(await screen.findByText("Partial form record")).toBeVisible();
  expect(screen.getByText(/not attributed to a specific application attempt/)).toBeVisible();
  expect(screen.getByText("555-0100")).toBeVisible();
  expect(screen.getByText("Written, not verified")).toBeVisible();
  expect(screen.getByText("Previously written")).toBeVisible();
  expect(screen.getByText("Boston")).toBeVisible();
  expect(screen.getByText("New York")).toBeVisible();
  expect(screen.getByText("No resume was recorded for this capture.")).toBeVisible();
});

test("shows loading, missing records, and a readable retry result after a failed read", async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error("offline"));
  renderForm();
  expect(screen.getByRole("status", { name: "Loading saved application form" })).toBeVisible();
  expect(await screen.findByText("The saved form could not be loaded.")).toBeVisible();
  vi.mocked(apiFetch).mockResolvedValue({ job_id: 42, status: "missing", reason: "This run predates capture.", snapshots: [] });
  fireEvent.click(screen.getByRole("button", { name: "Retry loading form" }));
  expect(await screen.findByText(/No saved form for this run/)).toHaveTextContent("This run predates capture.");
});

test("retains the last read with a stale warning when refresh fails", async () => {
  let failed = false;
  vi.mocked(apiFetch).mockImplementation(async () => {
    if (failed) throw new Error("offline");
    return response();
  });
  renderForm();
  expect(await screen.findByText("applicant@example.test")).toBeVisible();
  failed = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh saved form" }));
  expect(await screen.findByText("Saved form refresh failed")).toBeVisible();
  expect(screen.getByText("applicant@example.test")).toBeVisible();
  expect(screen.getByText(/These saved details may be stale/)).toBeVisible();
});

test("switching applications never shows the previous job's captured values", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response());
  const { rerender, client } = renderForm();
  expect(await screen.findByText("applicant@example.test")).toBeVisible();
  vi.mocked(apiFetch).mockResolvedValue({ job_id: 43, status: "unavailable", reason: "Saved source does not match this run.", snapshots: [] });
  rerender(<QueryClientProvider client={client}><ApplicationFormSnapshot batchId="batch-b" jobId={43} /></QueryClientProvider>);
  expect(screen.queryByText("applicant@example.test")).not.toBeInTheDocument();
  expect(await screen.findByText("Saved source does not match this run.")).toBeVisible();
});

test("does not turn captured text or unsafe source and artifact URLs into executable links", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response([snapshot({ source_url: "javascript:alert(1)", fields: [field({ value: "<script>alert(1)</script>", page_url: "javascript:alert(1)" })],
    resume: { filename: "missing.pdf", label: null, artifact_url: "https://untrusted.example.test/resume.pdf", upload_status: "unknown" },
  })]));
  renderForm();
  const region = screen.getByRole("region", { name: "Saved application form" });
  expect(await within(region).findByText("<script>alert(1)</script>")).toBeVisible();
  expect(within(region).getByText("The saved resume file is unavailable.")).toBeVisible();
  expect(within(region).queryByRole("link")).not.toBeInTheDocument();
});

test("does not present an unbound current resume selection as the resume used for an older form", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response([snapshot({ id: "resume-selection", captured_at: null, attempt_id: null, owner_thread_id: null,
    provenance: "legacy", coverage: "partial_history", fields: [],
    resume: { filename: "selected-only.pdf", label: null, artifact_url: null, upload_status: "selected" },
  })]));
  renderForm();
  expect(await screen.findByRole("heading", { name: "Current selected resume" })).toBeVisible();
  expect(screen.getByText("This selection is not linked to a saved form capture or a verified upload.")).toBeVisible();
  expect(screen.getByText("selected-only.pdf")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Resume used" })).not.toBeInTheDocument();
});
