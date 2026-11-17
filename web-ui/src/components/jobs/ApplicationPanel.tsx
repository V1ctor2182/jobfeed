import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { useApplicationForm } from "@/api/application-forms";
import { apiPost } from "@/api/client";
import { jobsKeys, useJobDetail } from "@/api/queries";
import type { TrackerApplication, TrackerMessage } from "@/api/tracker";
import { STAGES, initials } from "@/components/jobs/ApplicationBoard";
import { ApplicationFormSnapshot } from "@/components/runs/ApplicationFormSnapshot";
import { formatLocalDateTime, formatRelativeAge } from "@/lib/dates";

type Tab = "overview" | "resume" | "submission" | "emails";
const MAIL_LABEL: Record<TrackerMessage["status"], string> = {
  applied: "Received", action: "Needs action", update: "Update", interviewing: "Interview", rejected: "Rejected", offer: "Offer",
};

interface PanelProps {
  application: TrackerApplication;
  messages: TrackerMessage[];
  onClose: () => void;
  /** Set for a job that has not been applied to yet. It names where the job stands and hides what only an application has. */
  before?: { status: string; traceJobId?: number };
}

/** Everything known about one job: where it stands, notes and posting, and once applied the résumé, form and email. */
export function ApplicationPanel({ application, messages, onClose, before }: PanelProps) {
  const [tab, setTab] = useState<Tab>("overview");
  const jobId = application.job_id ?? null;
  const tabs: { id: Tab; label: string }[] = before ? [{ id: "overview", label: "Overview" }] : jobId === null
    ? [{ id: "overview", label: "Overview" }, { id: "emails", label: `Emails ${messages.length}` }]
    : [{ id: "overview", label: "Overview" }, { id: "resume", label: "Résumé" }, { id: "submission", label: "Submission" }, { id: "emails", label: `Emails ${messages.length}` }];
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const name = application.company ?? "Application";

  return (
    <div className="jf-panel-back" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="jf-panel" role="dialog" aria-modal="true" aria-label={`${name}${application.title ? ` · ${application.title}` : ""}`}>
        <header>
          <span className="jf-avatar large" aria-hidden="true">{initials(name)}</span>
          <div className="jf-panel-title"><h2>{name}</h2><div>{application.title ?? "Found in email only"}</div></div>
          {application.url && <a className="jf-panel-link" href={application.url} target="_blank" rel="noreferrer">Job posting ↗</a>}
          <button type="button" className="jf-panel-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <div className="jf-panel-tabs" role="tablist">
          {tabs.map(({ id, label }) => <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
        </div>
        <div className="jf-panel-body">
          {tab === "overview" && <Overview application={application} latest={messages[0]} before={before} />}
          {tab === "resume" && jobId !== null && <Resume jobId={jobId} batchId={application.batch_id ?? null} />}
          {tab === "submission" && jobId !== null && (application.batch_id
            ? <ApplicationFormSnapshot jobId={jobId} batchId={application.batch_id} />
            : <p className="jf-panel-note">No Applier run is recorded for this application, so there is no saved form.</p>)}
          {tab === "emails" && <Emails messages={messages} />}
        </div>
      </div>
    </div>
  );
}

function Overview({ application, latest, before }: { application: TrackerApplication; latest: TrackerMessage | undefined; before?: PanelProps["before"] }) {
  const jobId = application.job_id ?? null;
  const detail = useJobDetail(jobId === null ? null : String(jobId));
  const stage = STAGES.find(({ id }) => id === application.stage)?.label ?? application.stage;
  return (
    <>
      <h3>{before ? "Job details" : "Application details"}</h3>
      <dl className="jf-facts">
        <div><dt>Status</dt><dd><span className={`jf-stage ${before ? "" : application.stage}`}>{before?.status ?? stage}</span></dd></div>
        {!before && <>
          <div><dt>Date applied</dt><dd>{application.applied_at ? formatLocalDateTime(application.applied_at) : "–"}</dd></div>
          <div><dt>Last activity</dt><dd>{application.last_activity_at ? `${formatRelativeAge(application.last_activity_at)} ago` : "–"}</dd></div>
          <div><dt>Confirmation email</dt><dd>{application.confirmed_by_email ? "Received" : "None found"}</dd></div>
        </>}
        {before && detail.data?.job.url && <div className="wide"><dt>Job URL</dt><dd><a href={detail.data.job.url} target="_blank" rel="noreferrer">{detail.data.job.url}</a></dd></div>}
        {application.url && <div className="wide"><dt>Job URL</dt><dd><a href={application.url} target="_blank" rel="noreferrer">{application.url}</a></dd></div>}
      </dl>
      {before?.traceJobId !== undefined && (
        <Link className="jf-panel-row link" to={`/applier-trace?job=${before.traceJobId}`}><span>What the Applier is doing</span><span>Open trace ↗</span></Link>
      )}
      {!before && jobId !== null && application.batch_id && (
        <Link className="jf-panel-row link" to={`/runs?tab=all&kind=application&run=${encodeURIComponent(application.batch_id)}&job=${jobId}`}>
          <span>Applier run and submission evidence</span><span>Open ↗</span>
        </Link>
      )}
      {jobId !== null && <Notes jobId={jobId} notes={detail.data?.status.notes ?? null} />}
      {detail.data && detail.data.status.history.length > 0 && (
        <div className="jf-panel-row"><span>Status history</span><span>{detail.data.status.history.join(" → ")}</span></div>
      )}
      {!before && <>
        <h3>Latest email</h3>
        {latest
          ? <div className="jf-panel-mail"><b>{latest.subject}</b><span>{MAIL_LABEL[latest.status]} · {formatRelativeAge(latest.received_at)} ago</span><p>{latest.snippet}</p></div>
          : <p className="jf-panel-note">No email is linked to this application.</p>}
      </>}
      {jobId !== null && <>
        <h3>Job description</h3>
        {detail.isPending && <p className="jf-panel-note" role="status">Loading the posting.</p>}
        {detail.isError && <p className="jf-panel-note" role="alert">The posting could not be loaded.</p>}
        {detail.data && <div className="jf-panel-jd">{detail.data.job.jd_text || "No description was saved for this posting."}</div>}
      </>}
    </>
  );
}

function Notes({ jobId, notes }: { jobId: number; notes: string | null }) {
  const client = useQueryClient();
  const [text, setText] = useState("");
  const add = useMutation({
    mutationFn: (note: string) => apiPost<unknown>(`/api/jobs/${jobId}/note`, { text: note }),
    onSuccess: () => { setText(""); void client.invalidateQueries({ queryKey: jobsKeys.detail(String(jobId)) }); },
  });
  return (
    <form className="jf-panel-notes" onSubmit={(event) => { event.preventDefault(); if (text.trim()) add.mutate(text.trim()); }}>
      <label htmlFor={`note-${jobId}`}>Notes</label>
      {notes && <div className="saved">{notes}</div>}
      <textarea id={`note-${jobId}`} rows={2} value={text} placeholder="Interview scheduled, recruiter name, etc." onChange={(event) => setText(event.target.value)} />
      <div><button type="submit" disabled={add.isPending || !text.trim()}>Add note</button>{add.isError && <span role="alert"> The note was not saved.</span>}</div>
    </form>
  );
}

function Resume({ jobId, batchId }: { jobId: number; batchId: string | null }) {
  if (batchId === null) return <p className="jf-panel-note">No Applier run is recorded for this application, so there is no saved résumé.</p>;
  return <SavedResume jobId={jobId} batchId={batchId} />;
}

function SavedResume({ jobId, batchId }: { jobId: number; batchId: string }) {
  const form = useApplicationForm(jobId, batchId);
  const resume = form.data?.snapshots?.map((snapshot) => snapshot.resume).find((item) => item?.artifact_url);
  if (form.isPending) return <p className="jf-panel-note" role="status">Loading the saved résumé.</p>;
  if (!resume?.artifact_url) return <p className="jf-panel-note">No résumé file was saved for this application.</p>;
  return (
    <div className="jf-panel-resume">
      <div><b>{resume.label ?? resume.filename}</b><a href={resume.artifact_url} target="_blank" rel="noreferrer">Open in new tab ↗</a></div>
      <iframe title="Résumé sent with this application" src={resume.artifact_url} />
    </div>
  );
}

function Emails({ messages }: { messages: TrackerMessage[] }) {
  if (messages.length === 0) return <p className="jf-panel-note">No email is linked to this application.</p>;
  return (
    <ul className="jf-panel-mails">
      {messages.map((message) => (
        <li key={message.id} className="jf-panel-mail">
          <b>{message.subject}</b>
          <span>{MAIL_LABEL[message.status]} · {formatLocalDateTime(message.received_at)}{message.view_url && <> · <a href={message.view_url} target="_blank" rel="noreferrer">Open in Gmail ↗</a></>}</span>
          <p>{message.snippet}</p>
        </li>
      ))}
    </ul>
  );
}
