import { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button, { type ButtonProps } from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useControllerHandoff, type HandoffRuntime } from "@/api/application-handoffs";
import type { QueueAdmissionReceipt } from "@/api/application-queue";
import type { QueueJob } from "@/api/workspace";
import { RouteLink } from "./shared";
import { agentChatUrl } from "@/components/runs/run-model";

// Apply starts Claude Appliers. Codex wording remains only to show a request saved before the switch.
const statusText = {
  codex: {
    header: "Controller handoff status",
    labels: {
      queued: "Handoff recorded. Controller dispatch has not been confirmed.",
      waiting_for_capacity: "This handoff is blocked. Selected jobs have not been dispatched. It will not retry automatically.",
      starting: "Handoff accepted. The controller is starting to dispatch the selected jobs.",
      preparing: "Jobs handed to the controller. It dispatches them as capacity becomes available. Review progress in Application runs.",
      ready_for_review: "Application drafts are ready for review.",
      needs_attention: "The handoff needs attention. It will not retry automatically.",
      unknown_delivery: "Controller delivery is unconfirmed. Check status before taking another action.",
    },
  },
  claude: {
    header: "Apply status",
    labels: {
      queued: "Apply recorded. The application worker has not started yet.",
      waiting_for_capacity: "These jobs have not started. This will not retry automatically.",
      starting: "The application worker is starting for these jobs.",
      preparing: "Claude Appliers are filling these forms in your Chrome. Their questions arrive in Discord. Review progress in Application runs.",
      ready_for_review: "The Claude Appliers have finished these jobs.",
      needs_attention: "These jobs need attention. Nothing will retry automatically.",
      unknown_delivery: "The request is unconfirmed. Check status before taking another action.",
    },
  },
} satisfies Record<HandoffRuntime, { header: string; labels: Record<QueueAdmissionReceipt["status"], string> }>;

export function ControllerHandoff({ selectedJobs, onAcknowledged, onClear }: {
  selectedJobs: QueueJob[]; onAcknowledged: (ids: number[]) => void; onClear: () => void;
}) {
  const handoff = useControllerHandoff();
  const [review, setReview] = useState<QueueJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<ButtonProps.Ref>(null);
  const acknowledgedId = useRef<string | null>(null);
  const request = handoff.request;
  const controllerUrl = request?.runtime === "claude" ? null : agentChatUrl(request?.controller_thread_id);
  const sending = handoff.attempt?.state === "sending";
  const unresolved = Boolean(handoff.attempt && (handoff.attempt.state !== "acknowledged" || !request))
    || request?.status === "unknown_delivery";
  const unavailable = !handoff.control.data?.csrf_token || handoff.control.isError || handoff.status.isError || unresolved;

  useEffect(() => {
    if (!request || acknowledgedId.current === request.request_id || !["starting", "preparing", "ready_for_review"].includes(request.status)) return;
    acknowledgedId.current = request.request_id;
    onAcknowledged(request.items.filter(item => item.status !== "rejected").map(item => item.source_job_id).filter((id): id is number => typeof id === "number"));
  }, [request, onAcknowledged]);

  const close = () => {
    setReview(null);
    requestAnimationFrame(() => trigger.current?.focus());
  };
  const confirm = async () => {
    if (!review || unavailable || sending) return;
    setError(null);
    try {
      await handoff.submit(review.map(job => job.job_id), "claude");
      close();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request could not be confirmed.");
      close();
    }
  };

  return <SpaceBetween size="s">
    <div role="toolbar" aria-label="Apply to queued jobs">
      <SpaceBetween direction="horizontal" size="s">
        <Box variant="strong">{selectedJobs.length} selected</Box>
        <Button ref={trigger} variant="primary" loading={sending} disabled={unavailable || selectedJobs.length === 0 || selectedJobs.length > 100}
          onClick={() => { setError(null); setReview(selectedJobs.map(job => ({ ...job }))); }}>Apply</Button>
        {selectedJobs.length > 0 && <Button variant="link" disabled={sending} onClick={onClear}>Clear queue selection</Button>}
      </SpaceBetween>
    </div>
    <Box variant="small">Only queued jobs without an assigned applier can be selected. Apply starts a Claude Applier for each job. It fills the form and stops before Submit.</Box>
    {selectedJobs.length > 100 && <Alert type="info">Choose up to 100 queued jobs at a time.</Alert>}
    {handoff.attempt?.state === "uncertain" && <Alert type="warning" header="Request unconfirmed">No duplicate request will be sent. Checking status only reads the saved request.</Alert>}
    {request && <Alert type={["needs_attention", "waiting_for_capacity", "unknown_delivery"].includes(request.status) ? "warning" : "info"} header={statusText[request.runtime].header}>
      <SpaceBetween size="xs">
        <Box>{statusText[request.runtime].labels[request.status]}{request.reason ? ` ${request.reason}` : ""}</Box>
        {request.items.filter(item => item.status === "rejected").map((item, index) => <Box key={`${item.source_job_id ?? item.real_job_id}-${index}`}>
          Job {item.source_job_id ?? item.real_job_id ?? "not identified"}: {item.reason || "This job could not be started."}
        </Box>)}
        <RouteLink href="/runs?tab=all">View application runs</RouteLink>
        {controllerUrl && <Link href={controllerUrl}>{request.status === "waiting_for_capacity" ? "Open existing controller chat" : "Open controller chat"}</Link>}
      </SpaceBetween>
    </Alert>}
    {error && !unresolved && <Alert type="error" header="Apply could not be completed">{error}</Alert>}
    {(handoff.control.isError || handoff.status.isError) && <Alert type="warning" header="Status unavailable">Refresh the saved status before applying.</Alert>}
    {(handoff.attempt || handoff.control.isError) && <Button loading={handoff.status.isFetching || handoff.control.isFetching} onClick={() => {
      void handoff.control.refetch();
      if (handoff.attempt) void handoff.status.refetch();
    }}>Check status</Button>}
    {review && <Modal visible onDismiss={close} closeAriaLabel="Close apply review" header={`Apply to ${review.length} ${review.length === 1 ? "job" : "jobs"}`} size="medium"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="s">
        <Button variant="link" disabled={sending} onClick={close}>Cancel</Button>
        <Button variant="primary" loading={sending} disabled={unavailable} onClick={() => { void confirm(); }}>Confirm and apply</Button>
      </SpaceBetween></Box>}>
      <SpaceBetween size="m">
        <Box>A Claude Applier fills each form in your Chrome and stops before Submit. Its questions arrive in Discord. You confirm each Submit in Application runs. One application worker runs at a time: if another worker is already running, nothing starts and you can try again after it stops.</Box>
        <ul aria-label="Jobs to apply to">{review.map(job => <li key={job.job_id}>
          <span className="workspace-wrap">{job.company || "Company not recorded"} · {job.title || "Title not recorded"} · Source {job.job_id}</span>
        </li>)}</ul>
      </SpaceBetween>
    </Modal>}
  </SpaceBetween>;
}
