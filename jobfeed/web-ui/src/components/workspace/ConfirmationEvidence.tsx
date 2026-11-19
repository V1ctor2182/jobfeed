import { useEffect, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { apiFetch } from "@/api/client";
import type { components } from "@/api/types.gen";
import { timestamp } from "./shared";

type Confirmation = components["schemas"]["ConfirmationResponse"];

export function ConfirmationEvidence({ jobId }: { jobId: number }) {
  return <ConfirmationContent key={jobId} jobId={jobId} />;
}

function ConfirmationContent({ jobId }: { jobId: number }) {
  const [result, setResult] = useState<Confirmation | null>(null);
  const [failed, setFailed] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    apiFetch<Confirmation>(`/api/apply-health/jobs/${jobId}/confirmation`, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setResult(data); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [jobId, revision]);
  if (failed) return <Alert type="warning" header="Confirmation unavailable" action={<Button onClick={() => { setResult(null); setFailed(false); setRevision(revision + 1); }}>Retry</Button>}>The saved evidence could not be loaded. Submission is unverified.</Alert>;
  if (!result) return <div role="status">Loading confirmation evidence…</div>;
  if (result.status !== "available") return <Alert type="warning" header="Submission unverified">{result.reason || "No valid saved confirmation evidence is available. Do not resubmit automatically."}</Alert>;
  const screenshot = result.kind === "website_screenshot";
  const artifact = `/api/apply-health/jobs/${jobId}/confirmation/artifact`;
  return <SpaceBetween size="m">
    <Header variant="h3">{screenshot ? "Confirmation screenshot" : "Confirmation email"}</Header>
    <Box color="text-body-secondary">Saved source evidence is available for inspection. It is separate from the agent’s summary and database sync.</Box>
    <dl className="workspace-detail">
      <dt>Captured</dt><dd>{timestamp(result.captured_at)}</dd>
      <dt>Source</dt><dd>{result.source_url && /^https?:\/\//i.test(result.source_url) ? <Link external href={result.source_url}>Open confirmation source</Link> : "Not recorded"}</dd>
      <dt>Capture tool</dt><dd>{result.capture_tool || "Not recorded"}</dd>
      <dt>Capture reference</dt><dd>{result.source_id || "Not recorded"}</dd>
      <dt>Confirmation text</dt><dd>{result.confirmation_text}</dd>
    </dl>
    {screenshot ? <>
      <a href={artifact} target="_blank" rel="noopener noreferrer">Open full screenshot</a>
      {imageFailed ? <Alert type="warning" header="Screenshot unavailable">The saved image could not be displayed. Reload the evidence before relying on this confirmation.</Alert> : <img className="workspace-confirmation-image" src={artifact} onError={() => setImageFailed(true)} alt={`Saved application confirmation for job ${jobId}`} />}
    </> : result.email && <>
      <dl className="workspace-detail">
        <dt>From</dt><dd>{result.email.sender}</dd>
        <dt>To</dt><dd>{result.email.recipient}</dd>
        <dt>Subject</dt><dd>{result.email.subject}</dd>
        <dt>Email date</dt><dd>{result.email.date}</dd>
        <dt>Message ID</dt><dd>{result.email.message_id}</dd>
      </dl>
      <pre className="workspace-confirmation-email">{result.email.body}</pre>
    </>}
  </SpaceBetween>;
}


export function ConfirmationLookup() {
  const [sourceId, setSourceId] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const valid = /^[1-9]\d*$/.test(sourceId) && Number.isSafeInteger(Number(sourceId));
  return <Container header={<Header variant="h2" description="Inspect saved confirmations, including completed jobs that have left the queue. Find the source ID in the job’s queue details or saved application record.">Find application confirmation</Header>}>
    <SpaceBetween size="m">
      <FormField label="Job source ID"><Input value={sourceId} inputMode="numeric" ariaLabel="Job source ID" onChange={({ detail }) => setSourceId(detail.value)} /></FormField>
      <Button disabled={!valid} onClick={() => setSelected(Number(sourceId))}>View confirmation</Button>
      {selected !== null && <ConfirmationEvidence jobId={selected} />}
    </SpaceBetween>
  </Container>;
}
