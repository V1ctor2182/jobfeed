import { useState } from "react";

import { isApplicationsNotInstalled } from "@/api/client";
import { useApplierRuns } from "@/api/queries";
import { useTracker, type TrackerApplication } from "@/api/tracker";
import { useApplyHealth } from "@/api/workspace";
import { Board } from "@/components/jobs/ApplicationBoard";
import { ApplicationPanel } from "@/components/jobs/ApplicationPanel";
import { JOB_COLUMNS, jobCards, mailFor, matches } from "@/components/jobs/boards";
import "@/routes/jobs.css";

/** Scanned jobs on their way to an application: queued, being applied to, and applied. A card opens that job. */
export default function JobsPage() {
  const apply = useApplyHealth();
  const runs = useApplierRuns();
  const tracker = useTracker();
  const [search, setSearch] = useState("");
  const [openKey, setOpenKey] = useState<string | null>(null);
  const queue = apply.data?.queue;
  const cards = jobCards(queue?.state === "available" ? queue.jobs ?? [] : [], runs.data?.runs ?? [], tracker.data?.applications ?? []);
  const shown = cards.filter((card) => matches(search, card.company, card.title));
  const open = cards.find((card) => card.key === openKey);
  // The queue API returns a bounded list, so the true size of Queued comes from its per-state counts.
  const queuedTotal = Object.entries(queue?.counts_by_state ?? {}).find(([state]) => state.toLowerCase() === "queued")?.[1];
  const columns = JOB_COLUMNS.map((column) => column.id === "queued" && !search.trim() && queuedTotal !== undefined
    ? { ...column, count: Math.max(queuedTotal, shown.filter((card) => card.column === "queued").length) } : column);
  const loading = apply.isPending || runs.isPending || tracker.isPending;
  const failed = [apply.isError && "the queue", runs.isError && "Applier runs", tracker.isError && !isApplicationsNotInstalled(tracker.error) && "applied jobs"].filter(Boolean);
  const placeholder = (jobId: number, company: string, title: string): TrackerApplication => ({
    job_id: jobId, company, title, source: "jobfeed", stage: "applied", confirmed_by_email: false, email_count: 0,
  });

  return (
    <div className="jf-jobs">
      <div className="jf-jobs-bar">
        <p>Jobs the scan found and you or autopilot sent to apply. Progress after applying is on the Tracker.</p>
        <input type="search" aria-label="Search companies" placeholder="Search companies" value={search} onChange={(event) => setSearch(event.target.value)} />
      </div>
      {loading && <p role="status" className="jf-jobs-note">Loading jobs.</p>}
      {failed.length > 0 && <p role="alert" className="jf-jobs-note">Could not load {failed.join(" and ")}, so some columns may be incomplete.</p>}
      {!loading && <Board columns={columns} cards={shown} onOpen={setOpenKey} />}
      {open && (open.application
        ? <ApplicationPanel application={open.application} messages={mailFor(open.application, tracker.data?.messages ?? [])} onClose={() => setOpenKey(null)} />
        : <ApplicationPanel application={placeholder(open.jobId, open.company, open.title)} messages={[]} onClose={() => setOpenKey(null)}
            before={{ status: open.status, traceJobId: open.column === "applying" ? open.jobId : undefined }} />)}
    </div>
  );
}
