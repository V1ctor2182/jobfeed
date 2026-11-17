import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button, { type ButtonProps } from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { ApiError } from "@/api/client";
import { confirmSubmission, getSubmissionPreview, getSubmissionStatus, type PreviewResponse, type SubmissionRequest } from "@/api/application-submissions";

const requestMessages: Record<string, string> = {
  preparing: "Confirmation saved. Waiting to send to your application agent.",
  queued: "Submission request queued for your application agent.",
  running: "Your application agent is checking and submitting this application.",
  unknown: "Delivery is unconfirmed. Check this request; do not submit again.",
  turn_ended: "The application agent finished its turn. Submission still needs verification.",
  blocked: "This submission needs attention. Check the existing request before continuing.",
  expired: "This submission request expired. Check its outcome before continuing.",
};

function submissionMessage(request: SubmissionRequest | undefined): string {
  if (!request) return "Checking your existing submission request. Do not submit again.";
  if (!request.submitted && request.state === "preparing") return request.reason || requestMessages.preparing!;
  const message = request.submitted
    ? `Website submission verified. ${request.synced === true ? "Jobfeed sync verified." : "Jobfeed sync pending."}`
    : request.state === "blocked" && request.delivery_not_started === true
      ? "Review needed. This request was not sent to the application agent."
      : requestMessages[request.state] ?? "Submission status is unconfirmed. Check this request; do not submit again.";
  return `${message}${request.reason ? ` ${request.reason}` : ""}`;
}

export function ApplicationSubmitButton({ batchId, jobId }: { batchId: string; jobId: number }) {
  const client = useQueryClient();
  const storageKey = JSON.stringify(["application-submission", batchId, jobId]);
  const [requestId, setRequestId] = useState<string | null>(() => { try { return sessionStorage.getItem(storageKey); } catch { return null; } });
  const [review, setReview] = useState<PreviewResponse | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const button = useRef<ButtonProps.Ref>(null);
  const preview = useQuery({ queryKey: ["application-submit-preview", batchId, jobId], queryFn: () => getSubmissionPreview(batchId, jobId), retry: false, refetchInterval: 30_000 });
  const status = useQuery<SubmissionRequest>({ queryKey: ["application-submit-request", requestId], queryFn: () => getSubmissionStatus(requestId!), enabled: !!requestId, retry: false, refetchInterval: requestId ? 5_000 : false });
  const close = () => { setReview(null); button.current?.focus(); };
  const open = async () => {
    setError(null);
    const fresh = await preview.refetch();
    if (fresh.data?.eligible && !fresh.error) setReview(fresh.data);
  };
  const reviewAgain = async () => {
    if (busy.current || status.data?.state !== "blocked" || status.data.delivery_not_started !== true) return;
    busy.current = true;
    setError(null);
    try {
      const current = await status.refetch();
      if (current.error || current.data?.state !== "blocked" || current.data.delivery_not_started !== true) return;
      const fresh = await preview.refetch();
      if (!fresh.data?.eligible || fresh.error) return;
      sessionStorage.removeItem(storageKey);
      setRequestId(null);
      setReview(fresh.data);
    } catch {
      setError("Cannot prepare a new review. Your existing request is preserved.");
    } finally { busy.current = false; }
  };
  const confirm = async () => {
    if (!review || busy.current || requestId) return;
    try { const existing = sessionStorage.getItem(storageKey); if (existing) { setRequestId(existing); close(); return; } } catch { /* Storage write below must succeed before sending. */ }
    busy.current = true;
    const id = crypto.randomUUID();
    try { sessionStorage.setItem(storageKey, id); } catch { setError("Cannot preserve request identity. Enable session storage before confirming."); busy.current = false; return; }
    setRequestId(id); setSending(true); setError(null);
    try {
      const result = await confirmSubmission(review, id);
      client.setQueryData(["application-submit-request", id], result);
      close();
      void preview.refetch();
    } catch (caught) {
      let rejectedBeforeRecording = caught instanceof ApiError && [403, 413].includes(caught.status);
      if (caught instanceof ApiError && [409, 422].includes(caught.status)) {
        try {
          const recorded = await getSubmissionStatus(id);
          rejectedBeforeRecording = recorded.state === "not_found" && recorded.accepted === false;
        } catch { /* An uncertain result keeps its request identity. */ }
      }
      if (rejectedBeforeRecording) {
        sessionStorage.removeItem(storageKey); setRequestId(null);
        void preview.refetch();
      }
      setError(caught instanceof Error ? caught.message : "Submission delivery is unconfirmed.");
      close();
    } finally { setSending(false); busy.current = false; }
  };
  const changed = review && preview.data && JSON.stringify(review) !== JSON.stringify(preview.data);
  return <SpaceBetween size="xs">
    <Button ref={button} disabled={preview.isError || !preview.data?.eligible || !!requestId || sending} loading={preview.isFetching && !review} onClick={() => void open()}>Submit application</Button>
    {preview.data?.reason && <Box variant="small">{preview.data.reason}</Box>}
    {preview.isError && <Alert type="error">Submission eligibility unavailable. Refresh before continuing.</Alert>}
    {requestId && <div role="status">{submissionMessage(status.data)}</div>}
    {requestId && status.data?.state === "blocked" && status.data.delivery_not_started === true && <Button onClick={() => void reviewAgain()} loading={preview.isFetching || status.isFetching}>Review current application</Button>}
    {error && <Alert type="error">{error}</Alert>}
    {(requestId || preview.isError) && <Button variant="inline-link" onClick={() => { if (requestId) void status.refetch(); else void preview.refetch(); }}>Check submission status</Button>}
    {review && <Modal visible header="Confirm and submit application" closeAriaLabel="Close submission review" onDismiss={close} size="large" footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs"><Button onClick={close}>Cancel</Button><Button variant="primary" disabled={!!changed || !!requestId} loading={sending} onClick={() => void confirm()}>Confirm and submit</Button></SpaceBetween></Box>}>
      <SpaceBetween size="m">
        <Box variant="h3">{review.company} · {review.title}</Box>
        <Box>Job {review.job_id} · Current owner {review.owner}</Box>
        <Alert type="warning" header="Your confirmation authorizes final submission">Confirming approves the exact answer versions shown below and final submission for this job. Your request is saved for the existing application agent to check and submit when it can proceed safely. You do not need to stop other applications or confirm again while it waits. Security checks, CAPTCHA, and uncertain submission results still require resolution.</Alert>
        {review.warnings.map((warning, index) => <Alert type="warning" key={index}>{warning}</Alert>)}
        {review.answers.length > 0 ? review.answers.map(answer => <div key={`${answer.entry_id}-${answer.version}`}><Box variant="h4">{answer.question}</Box><Box variant="small">Version {answer.version} · {answer.state} · Scope: {JSON.stringify(answer.scope)}</Box><div className="workspace-wrap" style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</div></div>) : <Box>No narrative answers require review for this application.</Box>}
        {changed && <Alert type="warning">This application changed during review. Cancel and reopen to review the current version.</Alert>}
      </SpaceBetween>
    </Modal>}
  </SpaceBetween>;
}
