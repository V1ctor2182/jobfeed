import { useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormFieldControl from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useApplicationForm, type FormField, type FormSnapshot } from "@/api/application-forms";
import { LoadingSnapshot, timestamp } from "@/components/workspace/shared";
import { safeJobUrl } from "./run-model";
import "./ApplicationFormSnapshot.css";

const fieldStates = {
  observed: "Observed on form",
  written_unverified: "Written, not verified",
  unanswered: "Unanswered",
  unavailable: "Value unavailable",
} satisfies Record<FormField["state"], string>;

const uploadStates = {
  selected: "Selected resume. Upload not verified.",
  upload_pending: "Upload selected. Completion not verified.",
  uploaded: "Upload recorded.",
  unknown: "Upload status not recorded.",
} satisfies Record<NonNullable<FormSnapshot["resume"]>["upload_status"], string>;

function AnswerValue({ value }: { value: FormField["value"] }) {
  if (value === null || value === undefined) return <span>Not recorded</span>;
  if (value === "" || Array.isArray(value) && value.length === 0) return <span>Empty</span>;
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  return <span className="application-form-answer">{Array.isArray(value) ? value.join("\n") : String(value)}</span>;
}

function SavedResume({ resume, selectionOnly }: { resume: FormSnapshot["resume"]; selectionOnly: boolean }) {
  const artifact = resume?.artifact_url?.startsWith("/api/apply-health/") ? resume.artifact_url : null;
  return <SpaceBetween size="xs">
    <Header variant="h3">{selectionOnly ? "Current selected resume" : "Resume used"}</Header>
    {resume ? <SpaceBetween size="xs">
      <Box><span className="workspace-wrap">{resume.label || resume.filename}</span></Box>
      {resume.label && <Box variant="small"><span className="workspace-wrap">{resume.filename}</span></Box>}
      <Box variant="small">{uploadStates[resume.upload_status]}</Box>
      {artifact ? <SpaceBetween direction="horizontal" size="m">
        <Link external href={artifact}>Open saved resume</Link>
        <a href={artifact} download={resume.filename}>Download resume</a>
      </SpaceBetween> : <Box variant="small">The saved resume file is unavailable.</Box>}
    </SpaceBetween> : <Box>No resume was recorded for this capture.</Box>}
  </SpaceBetween>;
}

function SnapshotContent({ snapshot }: { snapshot: FormSnapshot }) {
  const source = safeJobUrl(snapshot.source_url);
  const selectionOnly = snapshot.id === "resume-selection";
  return <SpaceBetween size="m">
    {(snapshot.provenance === "legacy" || snapshot.coverage === "partial_history") && <Alert type="info" header="Partial form record">
      Only saved fields are shown. Other fields and manual edits may be missing.
      {snapshot.provenance === "legacy" && " These older records are not attributed to a specific application attempt."}
    </Alert>}
    <Box variant="small">{snapshot.captured_at ? `Saved ${timestamp(snapshot.captured_at)}.` : "Capture time not recorded."} {snapshot.coverage === "visible_controls"
      ? "Shows the visible fields captured on the recorded form pages. Hidden fields and later edits may not be included."
      : "Shows the available field history."}</Box>
    {source && <Link external href={source}>Recorded form page</Link>}
    <SavedResume resume={snapshot.resume} selectionOnly={selectionOnly} />
    {selectionOnly && <Box variant="small">This selection is not linked to a saved form capture or a verified upload.</Box>}
    <Header variant="h3">Form fields <span className="application-form-count">({snapshot.fields?.length ?? 0})</span></Header>
    {snapshot.fields?.length ? <dl className="application-form-fields">{snapshot.fields.map((field, index) => {
      const page = safeJobUrl(field.page_url);
      const hasWritten = field.written_value !== null && field.written_value !== undefined;
      const shownValue = field.state === "written_unverified" && hasWritten ? field.written_value : field.value;
      return <div className="application-form-field" key={`${field.id}-${index}`}>
        <dt>{field.question || "Unlabeled field"}</dt>
        <dd>
          <AnswerValue value={shownValue} />
          <div className="application-form-field-meta">
            <StatusIndicator type={field.state === "observed" ? "info" : "warning"}>{fieldStates[field.state]}</StatusIndicator>
            {field.section && <span>{field.section}</span>}
            {page && <Link external href={page}>Field source page</Link>}
          </div>
          {field.state !== "written_unverified" && hasWritten && JSON.stringify(field.written_value) !== JSON.stringify(field.value) && <div className="application-form-written">
            <Box variant="small">Previously written</Box><AnswerValue value={field.written_value} />
          </div>}
        </dd>
      </div>;
    })}</dl> : <Box>No fields were recorded in this capture.</Box>}
  </SpaceBetween>;
}

export function ApplicationFormSnapshot({ jobId, batchId }: { jobId: number; batchId: string }) {
  return <ApplicationFormContent key={`${batchId}-${jobId}`} jobId={jobId} batchId={batchId} />;
}

function ApplicationFormContent({ jobId, batchId }: { jobId: number; batchId: string }) {
  const query = useApplicationForm(jobId, batchId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const data = query.data;
  const snapshots = data?.snapshots ?? [];
  const selected = snapshots.find(snapshot => snapshot.id === selectedId) ?? snapshots[0];
  const options = snapshots.map((snapshot, index) => ({
    value: snapshot.id,
    label: snapshot.id === "resume-selection" ? "Current selected resume" : `${index === 0 ? "Latest capture" : "Earlier capture"} · ${timestamp(snapshot.captured_at)}`,
    description: snapshot.provenance === "legacy" ? "Partial history · attempt not recorded" : snapshot.attempt_id || snapshot.owner_thread_id || "Saved form capture",
  }));
  return <section aria-label="Saved application form"><SpaceBetween size="m">
    <Header variant="h3" actions={<Button iconName="refresh" loading={query.isFetching} onClick={() => { void query.refetch(); }}>Refresh saved form</Button>}>Application form</Header>
    {query.isPending && <LoadingSnapshot label="Loading saved application form" />}
    {query.isError && <Alert type="warning" header={data ? "Saved form refresh failed" : "Saved form unavailable"}
      action={<Button onClick={() => { void query.refetch(); }}>Retry loading form</Button>}>
      {data ? "Showing the last successful read. These saved details may be stale." : "The saved form could not be loaded."}
    </Alert>}
    {data && <>
      {data.status === "missing" && <Box>No saved form for this run. {data.reason}</Box>}
      {data.status === "unavailable" && <Alert type="warning" header="Saved form unavailable">{data.reason || "The saved form could not be read."}</Alert>}
      {data.status === "partial" && data.reason && <Box color="text-body-secondary">{data.reason}</Box>}
      {snapshots.length > 1 && <FormFieldControl label="Saved capture">
        <Select options={options} selectedOption={options.find(option => option.value === selected?.id) ?? null}
          onChange={({ detail }) => setSelectedId(detail.selectedOption.value ?? null)} />
      </FormFieldControl>}
      {selected && <SnapshotContent snapshot={selected} />}
      {!selected && data.status !== "missing" && data.status !== "unavailable" && <Box>No form captures are available for this run.</Box>}
    </>}
  </SpaceBetween></section>;
}
