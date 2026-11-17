import { useJobStars } from "@/api/job-stars";
import { useJobDetail } from "@/api/queries";
import { DetailPane } from "@/components/jobs/DetailPane";
import { StarButton } from "@/components/jobs/StarButton";
import { useCallback, useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import type { BatchHealth, ObservedJob, QueueJob } from "@/api/workspace";
import { label, timestamp } from "./shared";
import { ConfirmationEvidence } from "./ConfirmationEvidence";
import { ControllerHandoff } from "./ControllerHandoff";

export function ApplicationTable({ jobs, truncated, enableHandoff = false }: { jobs: QueueJob[]; truncated?: boolean; enableHandoff?: boolean }) {
  const [search, setSearch] = useState("");
  const [state, setState] = useState("all");
  const [priority, setPriority] = useState("all");
  const [hold, setHold] = useState("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [handoffIds, setHandoffIds] = useState<number[]>([]);
  const clearAcknowledged = useCallback((ids: number[]) => {
    const acknowledged = new Set(ids);
    setHandoffIds(previous => previous.filter(id => !acknowledged.has(id)));
  }, []);
  const eligible = (job: QueueJob) => job.queue_state.toLowerCase() === "queued" && !job.owner_thread_id && !job.hold;
  const handoffJobs = jobs.filter(job => handoffIds.includes(job.job_id) && eligible(job));
  const selected = jobs.find((job) => job.job_id === selectedId);
  const options = [{ value: "all", label: "All states" }, ...Array.from(new Set(jobs.map((job) => job.queue_state))).sort().map((value) => ({ value, label: label(value) }))];
  const priorityOptions = [{ value: "all", label: "All priorities" }, { value: "user", label: "User priority" }, { value: "standard", label: "Standard" }];
  const holdOptions = [{ value: "all", label: "All jobs" }, { value: "captcha", label: "CAPTCHA needed" }, { value: "none", label: "Not held" }];
  const filtered = jobs.filter((job) => (state === "all" || job.queue_state === state) && (priority === "all" || Boolean(job.manual_priority) === (priority === "user")) && (hold === "all" || (job.hold?.kind ?? "none") === hold) && `${job.title} ${job.company} ${job.job_id}`.toLowerCase().includes(search.toLowerCase()));
  const stars = useJobStars(filtered.map(job => job.job_id));
  return <SpaceBetween size="m">
    {enableHandoff && <ControllerHandoff selectedJobs={handoffJobs} onAcknowledged={clearAcknowledged} onClear={() => setHandoffIds([])} />}
    <Table items={filtered} trackBy="job_id" wrapLines
      selectionType={enableHandoff ? "multi" : undefined}
      selectedItems={enableHandoff ? filtered.filter(job => handoffIds.includes(job.job_id) && eligible(job)) : undefined}
      isItemDisabled={enableHandoff ? job => !eligible(job) : undefined}
      onSelectionChange={enableHandoff ? ({ detail }) => {
        const visible = new Set(filtered.map(job => job.job_id));
        setHandoffIds(previous => [...previous.filter(id => !visible.has(id)), ...detail.selectedItems.filter(eligible).map(job => job.job_id)]);
      } : undefined}
      ariaLabels={enableHandoff ? {
        tableLabel: "Application queue", selectionGroupLabel: "Queue job selection",
        allItemsSelectionLabel: () => "Select eligible visible queue jobs",
        itemSelectionLabel: (_state, job) => `Select queued ${job.company || "company"} ${job.title || job.job_id}`,
      } : undefined}
      header={<Header variant="h2" counter={`(${filtered.length})`} description={<>Jobs you add have user priority and appear first. Adding jobs does not start applications. Select queued jobs and click Apply when you are ready. Stars are saved bookmarks and do not change queue priority or start applications. A job marked CAPTCHA needed stays in the queue but waits until you solve the check in Chrome and press I solved it, retry in its Discord thread.{truncated && " Showing a bounded subset of the global queue. Filters apply to loaded rows."}</>}>Application queue</Header>}
      filter={<div className="workspace-filters">
        <FormField label="Search queue"><Input type="search" value={search} onChange={({ detail }) => setSearch(detail.value)} placeholder="Title, company, or source ID" ariaLabel="Search queue" /></FormField>
        <FormField label="Queue state"><Select ariaLabel="Queue state" selectedOption={options.find((option) => option.value === state)!} options={options} onChange={({ detail }) => setState(detail.selectedOption.value!)} /></FormField>
        <FormField label="Priority"><Select ariaLabel="Priority" selectedOption={priorityOptions.find((option) => option.value === priority)!} options={priorityOptions} onChange={({ detail }) => setPriority(detail.selectedOption.value!)} /></FormField>
        <FormField label="Hold"><Select ariaLabel="Hold" selectedOption={holdOptions.find((option) => option.value === hold)!} options={holdOptions} onChange={({ detail }) => setHold(detail.selectedOption.value!)} /></FormField>
      </div>}
      columnDefinitions={[
        { id: "star", header: "Star", minWidth: 60, cell: job => <StarButton sourceId={job.job_id} label={`${job.company} ${job.title}`} stars={stars} /> },
        { id: "job", minWidth: 180, header: "Job", cell: (job) => <><Button variant="inline-link" ariaExpanded={selectedId === job.job_id} ariaControls={`queue-job-details-${job.job_id}`} onClick={() => setSelectedId(selectedId === job.job_id ? null : job.job_id)}>{job.title || `Job ${job.job_id}`}</Button><div className="workspace-metadata">{job.company || "Company not recorded"} · Source {job.job_id}</div></> },
        { id: "state", minWidth: 140, header: "Queue state", cell: (job) => job.hold ? <>{label(job.queue_state)}<div className="workspace-metadata">CAPTCHA needed</div></> : label(job.queue_state) },
        { id: "priority", minWidth: 130, header: "Priority", cell: (job) => job.manual_priority ? "User priority" : "Standard" },
        { id: "retry", minWidth: 70, header: "Retries", cell: (job) => job.retry_count },
      ]}
      empty={<Box textAlign="center" padding="l">{jobs.length === 0 ? "No application jobs are queued." : "No jobs match these filters."}</Box>}
    />
    {selected && <Modal visible onDismiss={() => setSelectedId(null)} closeAriaLabel="Close details" header="Job details" size="large" position="top">
      <div id={`queue-job-details-${selected.job_id}`}><SpaceBetween size="l">
        <DetailPane jobId={String(selected.job_id)} />
        <PostingDescription jobId={String(selected.job_id)} />
        <ExpandableSection headerText="Queue record" variant="container" defaultExpanded>
          <dl className="workspace-detail">
            <dt>Job</dt><dd>{selected.title || `Job ${selected.job_id}`}</dd>
            <dt>Company</dt><dd>{selected.company || "Not recorded"}</dd>
            <dt>Source ID</dt><dd>{selected.job_id}</dd>
            <dt>Queue state</dt><dd>{label(selected.queue_state)}</dd>
            <dt>Priority</dt><dd>{selected.manual_priority ? "User priority" : "Standard"}</dd>
            <dt>Hold</dt><dd>{selected.hold ? <>CAPTCHA needed since {timestamp(selected.hold.created_at)}. {selected.hold.detail}{selected.hold.page_url && <> <Link href={selected.hold.page_url} external>Open the page</Link></>}</> : "None"}</dd>
            <dt>Recorded owner</dt><dd>{selected.owner_thread_id || "Not recorded"}</dd>
            <dt>Next retry</dt><dd>{timestamp(selected.next_retry_at)}</dd>
            <dt>Retry count</dt><dd>{selected.retry_count}</dd>
          </dl>
          <ConfirmationEvidence jobId={selected.job_id} />
        </ExpandableSection>
      </SpaceBetween></div>
    </Modal>}
  </SpaceBetween>;
}

/** The stored job description, which the shared job view only summarizes after evaluation. */
function PostingDescription({ jobId }: { jobId: string }) {
  const text = useJobDetail(jobId).data?.job.jd_text?.trim();
  if (!text) return null;
  return <ExpandableSection headerText="Job description" variant="container" defaultExpanded>
    <div className="workspace-posting-description">{text}</div>
  </ExpandableSection>;
}

// A cancelled or closed job, or one only paused because it was set aside, waits on nobody.
export function needsAttention(job: ObservedJob) {
  const state = (job.queue_state ?? "").replaceAll("_", " ").toLowerCase();
  if (job.terminal || ["cancelled", "unavailable"].includes(state)) return false;
  return Boolean(job.blocked) || (job.errors ?? []).length > 0
    || ["needs input", "needs verification", "submission unknown", "retry later", "awaiting approval"].includes(state);
}

export function ObservedJobs({ batch, attentionOnly = false }: { batch: BatchHealth; attentionOnly?: boolean }) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const jobs = (batch.jobs ?? []).filter((job) => !attentionOnly || needsAttention(job));
  const selected = jobs.find((job) => job.job_id === selectedId);
  const stars = useJobStars(jobs.map(job => job.job_id));
  return <SpaceBetween size="m">
    <Table items={jobs} trackBy="job_id" wrapLines
      header={<Header variant="h2" counter={`(${jobs.length})`} description={`Selected batch ${batch.id}. Observed ${timestamp(batch.checked_at)}.${batch.jobs_truncated ? " Only a bounded subset is shown." : ""}`}>{attentionOnly ? "Needs attention" : "Observed jobs"}</Header>}
      columnDefinitions={[
        { id: "star", header: "Star", minWidth: 60, cell: job => <StarButton sourceId={job.job_id} label={`${job.company} ${job.title}`} stars={stars} /> },
        { id: "job", minWidth: 180, header: "Job", cell: (job) => <><Button variant="inline-link" ariaExpanded={selectedId === job.job_id} ariaControls={`observed-job-details-${job.job_id}`} onClick={() => setSelectedId(selectedId === job.job_id ? null : job.job_id)}>{job.title || `Job ${job.job_id}`}</Button><div className="workspace-metadata">{job.company || "Company not recorded"} · Source {job.job_id}</div></> },
        { id: "state", minWidth: 140, header: "Observed state", cell: (job) => label(job.queue_state) },
        { id: "progress", minWidth: 180, header: "Progress or blocker", cell: (job) => label(job.blocked || job.errors?.[0] || job.next_step || job.turn_state) },
      ]}
      empty={<Box textAlign="center" padding="l">{batch.freshness !== "fresh" ? "No matching rows in this historical or unavailable observation. Current progress is unknown." : attentionOnly ? "No attention items recorded in this observation." : "No jobs recorded in this observation."}</Box>}
    />
    {selected && <Modal visible onDismiss={() => setSelectedId(null)} closeAriaLabel="Close details" header="Observation details" size="large" position="top">
      <div id={`observed-job-details-${selected.job_id}`}><ObservedJobDetails job={selected} /></div>
    </Modal>}
  </SpaceBetween>;
}

export function ObservedJobDetails({ job }: { job: ObservedJob }) {
  return <>
    <dl className="workspace-detail">
      <dt>Job</dt><dd>{job.title || `Job ${job.job_id}`}</dd>
      <dt>Source ID</dt><dd>{job.job_id}</dd>
      <dt>Recorded owner</dt><dd>{job.owner_thread_id || "Not recorded"}</dd>
      <dt>Observed state</dt><dd>{label(job.queue_state)}</dd>
      <dt>Turn state</dt><dd>{label(job.turn_state)}</dd>
      <dt>Blocker</dt><dd>{label(job.blocked)}</dd>
      <dt>Next step</dt><dd>{label(job.next_step)}</dd>
      <dt>Last tool activity</dt><dd>{timestamp(job.last_tool_at)}</dd>
      <dt>Last turn</dt><dd>{timestamp(job.turn_at)}</dd>
      <dt>Confirmation evidence</dt><dd>{job.submitted ? "Available" : "Submission unverified"}</dd>
      <dt>User confirmed submission</dt><dd>{job.user_submitted ? "Yes" : "No"}</dd>
      <dt>Synced</dt><dd>{job.synced ? "Yes" : "No"}</dd>
      <dt>Tab closure verified</dt><dd>{job.closed ? "Yes" : "No"}</dd>
      <dt>Errors recorded</dt><dd>{job.errors?.length ? job.errors.map(label).join(" · ") : "None recorded"}</dd>
    </dl>
    <ConfirmationEvidence jobId={job.job_id} />
  </>;
}
