import { useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button, { type ButtonProps } from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useApplicationRerun, type ApplicationRerun } from "@/api/application-reruns";
import type { ObservedJob } from "@/api/workspace";

const messages = {
  queued: "Rerun queued. Waiting for the applier to start.",
  unknown: "Rerun delivery is unconfirmed. Check status before taking another action.",
  running: "Applier is running.",
  completed: "Applier turn finished. Review its progress.",
  blocked: "Rerun needs attention.",
};

const phaseMessages: Record<NonNullable<ApplicationRerun["phase"]>, string> = {
  relay_pending: "Restart queued. Waiting to replace the previous form and applier.",
  cleanup: "Closing the previous application tab.",
  create_unknown: "Checking whether the new applier was created. No duplicate will be started.",
  candidate_received: "New applier created. Connecting it to this application.",
  adopting: "Connecting the new applier to this application.",
  adopted: "New applier connected. Waiting for form filling to start.",
  filling: "The new applier is running with instructions to fill the form and stop before Submit.",
  completed: "Applier turn finished. Review its progress and form before submitting.",
  blocked: "Restart needs attention.",
};

export function ApplicationRerunButton({ batchId, job }: { batchId: string; job: ObservedJob }) {
  const { status, attempt, canRetry, submit } = useApplicationRerun(batchId, job.job_id);
  const [ownerToConfirm, setOwnerToConfirm] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<ButtonProps.Ref>(null);
  const request = status.data?.request;
  const progress = request && (request.state === "blocked" ? (request.phase ? phaseMessages.blocked : messages.blocked)
    : request.state === "unknown" && request.phase !== "create_unknown" ? messages.unknown
    : request.phase ? phaseMessages[request.phase] : messages[request.state]);
  const pending = request?.state === "queued" || request?.state === "unknown" || request?.state === "running";
  const unavailable = !attempt && !pending && status.data?.eligible === false;
  const eligibilityReason = !attempt && !pending ? status.data?.reason : null;
  const owner = status.data?.owner_thread_id;
  const ownerMatches = Boolean(owner && (!ownerToConfirm || ownerToConfirm === owner));
  const disabled = !status.data?.csrf_token || !status.data?.eligible || !ownerMatches || Boolean(attempt) || pending || status.isError;
  const close = () => {
    setOwnerToConfirm(null);
    requestAnimationFrame(() => trigger.current?.focus());
  };
  const confirm = async () => {
    if (disabled || !ownerToConfirm) return;
    setError(null);
    try { await submit(ownerToConfirm); close(); }
    catch { setError("Could not confirm the rerun. Check its status below before trying again."); close(); }
  };
  return <>
    <Button ref={trigger} variant="normal" disabled={disabled} loading={attempt?.state === "sending"}
      loadingText="Requesting application rerun" onClick={() => { setError(null); setOwnerToConfirm(status.data!.owner_thread_id); }}>
      Rerun application
    </Button>
    <div aria-live="polite">
      {attempt?.state === "uncertain" ? <Box variant="small">Rerun delivery is unconfirmed. Checking status will not send another request.</Box>
        : request && <StatusIndicator type={request.state === "unknown" || request.state === "blocked" ? "warning" : request.state === "running" ? "in-progress" : "info"}>
          {progress}{request.reason && request.reason !== eligibilityReason ? ` ${request.reason}` : ""}
        </StatusIndicator>}
      {(unavailable || eligibilityReason) && <Box variant="small">{unavailable && "Rerun unavailable. "}{eligibilityReason}</Box>}
      {ownerToConfirm && !ownerMatches && <Box variant="small">Applier changed. Close this confirmation and try again.</Box>}
    </div>
    {canRetry && ownerMatches && <Button variant="inline-link" onClick={() => { setError(null); void submit(attempt!.owner).catch(() => setError("Could not confirm the rerun. Check its status below before trying again.")); }}>Retry rerun request</Button>}
    {error && <div role="alert">{error}</div>}
    {status.isError && <div role="alert">Application rerun status unavailable.</div>}
    {(attempt?.state === "uncertain" || request?.state === "unknown" || status.isError || error) && <Button variant="inline-link" loading={status.isFetching} onClick={() => void status.refetch()}>Check rerun status</Button>}
    {ownerToConfirm && <Modal visible onDismiss={close} closeAriaLabel="Close application rerun" header="Rerun application" size="medium"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={close}>Cancel</Button>
        <Button variant="primary" disabled={disabled} loading={attempt?.state === "sending"} onClick={() => void confirm()}>Rerun application</Button>
      </SpaceBetween></Box>}>
      <SpaceBetween size="m">
        <Box><strong>{job.title || "Untitled application"}</strong> at <strong>{job.company || "Company not recorded"}</strong></Box>
        <Box>Close the previous application tab and archive its applier chat. A new applier will open a fresh form for this application using the same resume and approved answers.</Box>
        <Alert type="info">The batch stays stopped. The applier will stop before Submit. Unapproved answers still require your review.</Alert>
      </SpaceBetween>
    </Modal>}
  </>;
}
