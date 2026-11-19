import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { apiFetch } from "@/api/client";
import { ConfirmationEvidence, ConfirmationLookup } from "./ConfirmationEvidence";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));
const fetchEvidence = vi.mocked(apiFetch);

afterEach(() => vi.resetAllMocks());

test("shows screenshot from the local job evidence route with source and capture time", async () => {
  fetchEvidence.mockResolvedValue({ job_id: 7, status: "available", kind: "website_screenshot", source_url: "https://careers.example.test/confirmation", captured_at: "2026-10-21T12:00:00Z", confirmation_text: "Your application was received.", capture_tool: "browser.screenshot", artifact_url: "https://untrusted.example/image.png" });
  render(<ConfirmationEvidence jobId={7} />);
  expect(await screen.findByRole("heading", { name: "Confirmation screenshot" })).toBeVisible();
  expect(screen.getByRole("img")).toHaveAttribute("src", "/api/apply-health/jobs/7/confirmation/artifact");
  expect(screen.getByRole("link", { name: "Open full screenshot" })).toHaveAttribute("href", "/api/apply-health/jobs/7/confirmation/artifact");
  expect(screen.getByText("Your application was received.")).toBeVisible();
});

test("renders original email as readable plain text without loading embedded content", async () => {
  fetchEvidence.mockResolvedValue({ job_id: 7, status: "available", kind: "confirmation_email", email: { sender: "careers@example.test", recipient: "candidate@example.test", subject: "Application received", message_id: "message-7", date: "2026-10-21", body: 'Application received. <img src="https://example.test/track">' } });
  render(<ConfirmationEvidence jobId={7} />);
  expect(await screen.findByRole("heading", { name: "Confirmation email" })).toBeVisible();
  expect(screen.getByText('Application received. <img src="https://example.test/track">')).toBeVisible();
  expect(screen.getByText("careers@example.test")).toBeVisible();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("keeps missing evidence unverified and never offers an application retry", async () => {
  fetchEvidence.mockResolvedValue({ job_id: 7, status: "unverified" });
  render(<ConfirmationEvidence jobId={7} />);
  expect(await screen.findByText("Submission unverified")).toBeVisible();
  expect(screen.getByText(/Do not resubmit automatically/)).toBeVisible();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("retries a failed evidence read", async () => {
  fetchEvidence.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ job_id: 7, status: "unverified" });
  render(<ConfirmationEvidence jobId={7} />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findByText("Submission unverified")).toBeVisible();
  expect(fetchEvidence).toHaveBeenCalledTimes(2);
});


test("warns if validated screenshot bytes are no longer available", async () => {
  fetchEvidence.mockResolvedValue({ job_id: 7, status: "available", kind: "website_screenshot", source_id: "capture-7" });
  render(<ConfirmationEvidence jobId={7} />);
  fireEvent.error(await screen.findByRole("img"));
  expect(screen.getByText("Screenshot unavailable")).toBeVisible();
  expect(screen.getByText("capture-7")).toBeVisible();
});

test("finds evidence by source ID after a completed job leaves the queue", async () => {
  fetchEvidence.mockResolvedValue({ job_id: 7, status: "available", kind: "confirmation_email", email: { subject: "Application received", body: "Your application was received." } });
  render(<ConfirmationLookup />);
  expect(screen.getByRole("button", { name: "View confirmation" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Job source ID" }), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "View confirmation" }));
  expect(await screen.findByRole("heading", { name: "Confirmation email" })).toBeVisible();
  expect(fetchEvidence).toHaveBeenCalledWith("/api/apply-health/jobs/7/confirmation", expect.anything());
});
