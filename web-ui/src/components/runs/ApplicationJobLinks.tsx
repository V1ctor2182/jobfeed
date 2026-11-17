import { useState } from "react";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import { ConfirmationEvidence } from "@/components/workspace/ConfirmationEvidence";
import Box from "@cloudscape-design/components/box";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ObservedJob } from "@/api/workspace";
import { useApplicationRerun } from "@/api/application-reruns";
import { agentChatUrl, isClaudeApplier, safeJobUrl } from "./run-model";
import { ApplicationSubmitButton } from "./ApplicationSubmitButton";
import { ApplicationRerunButton } from "./ApplicationRerunButton";
import { ApplicationTabJump } from "./ApplicationTabJump";

export function ApplicationJobLinks({ job, batchId }: { job: ObservedJob; batchId: string }) {
  return job.submitted ? <SubmittedJobLinks job={job} /> : <ActiveJobLinks job={job} batchId={batchId} />;
}

function SubmittedJobLinks({ job }: { job: ObservedJob }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const url = safeJobUrl(job.job_url);
  const chat = agentChatUrl(job.owner_thread_id);
  return <SpaceBetween size="xxs">
    <Box variant="small">Submitted{job.synced ? " · Jobfeed sync verified" : " · Jobfeed sync pending"}</Box>
    <ApplicationTabJump jobId={job.job_id} owner={job.owner_thread_id} />
    <Button variant="inline-link" onClick={() => setShowEvidence(true)}>View saved confirmation</Button>
    {url && <Link external href={url} externalIconAriaLabel="Opens job posting in a new tab">Open job posting</Link>}
    {chat && <Link href={chat}>Open applier chat</Link>}
    {isClaudeApplier(job.owner_thread_id) && <Box variant="small" color="text-body-secondary">Claude Applier · questions arrive on Discord</Box>}
    {showEvidence && <Modal visible header="Saved application confirmation" closeAriaLabel="Close saved confirmation" onDismiss={() => setShowEvidence(false)} size="large" footer={<Button onClick={() => setShowEvidence(false)}>Close confirmation</Button>}><ConfirmationEvidence jobId={job.job_id} /></Modal>}
  </SpaceBetween>;
}

function ActiveJobLinks({ job, batchId }: { job: ObservedJob; batchId: string }) {
  const url = safeJobUrl(job.job_url);
  const { status } = useApplicationRerun(batchId, job.job_id);
  const owner = status.data?.owner_thread_id ?? job.owner_thread_id;
  const chat = agentChatUrl(owner);
  const replacing = ["relay_pending", "cleanup", "create_unknown", "candidate_received", "adopting"].includes(status.data?.request?.phase ?? "");
  return <SpaceBetween size="xxs">
    {replacing ? <>
      {url && <div className="workspace-wrap">{url}</div>}
      <Box variant="small" color="text-body-secondary">The new application form will be available after the applier is connected.</Box>
    </> : <ApplicationTabJump key={`${job.job_id}-${owner}`} jobId={job.job_id} owner={owner} />}
    {url ? <Link external href={url} externalIconAriaLabel="Opens job posting in a new tab">Open job posting</Link>
      : <Box variant="small" color="text-body-secondary">Job URL unavailable</Box>}
    {chat ? <Link href={chat}>Open applier chat</Link>
      : <Box variant="small" color="text-body-secondary">{isClaudeApplier(owner) ? "Claude Applier · questions arrive on Discord" : "Applier chat unavailable"}</Box>}
    <ApplicationRerunButton batchId={batchId} job={job} />
    <ApplicationSubmitButton batchId={batchId} jobId={job.job_id} />
  </SpaceBetween>;
}
