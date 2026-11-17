import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Tabs from "@cloudscape-design/components/tabs";
import { useQueryClient } from "@tanstack/react-query";
import { realJobsKeys } from "@/api/queries";
import { Navigate, useLocation, useSearchParams } from "react-router";
import { useApplyHealth } from "@/api/workspace";
import { TriageDecisions } from "@/components/jobs/TriageDecisions";
import { ApplicationsOverview } from "@/components/runs/ApplicationsOverview";
import { CurrentRunsTable } from "@/components/runs/CurrentRunsTable";
import { SelectedRunDetail } from "@/components/runs/SelectedRunDetail";
import { applicationListItems, applicationStage, readSelection, selectionKey, type ApplicationStage, type RunSelection } from "@/components/runs/run-model";
import { ApplyReadStatus } from "@/components/workspace/ApplySummary";
import { RouteLink, timestamp } from "@/components/workspace/shared";
import { ApplicationQueueContent } from "./application-queue";
import LibraryPage from "./library";

const TAB_IDS = ["postings", "wait", "queue", "applying", "applied", "ignored", "all"] as const;
type ApplicationTab = typeof TAB_IDS[number];

function stageTab(stage: ApplicationStage | null): ApplicationTab {
  if (stage === "waiting") return "queue";
  return stage === "applied" ? "applied" : "applying";
}

export default function RunsPage() {
  const location = useLocation();
  const [params] = useSearchParams();
  if (params.get("kind") === "pipeline") return <Navigate to={{ pathname: "/scanner-runs", search: location.search }} replace />;
  return <ApplicationRunsPage />;
}

function ApplicationRunsPage() {
  const [params, setParams] = useSearchParams();
  const selection = readSelection(params);
  const applications = useApplyHealth();
  const client = useQueryClient();
  const batches = applications.data?.batches ?? [];
  const queue = applications.data?.queue;
  const items = applicationListItems(batches, queue?.state === "available" ? queue.jobs ?? [] : []);
  const selectedBatch = batches.find(batch => batch.id === selection?.id);
  const selectedJob = selection?.kind === "application" && selectedBatch?.source_binding === "valid"
    ? selectedBatch.jobs?.find(job => job.job_id === selection.jobId) : undefined;
  const selectedStage = selectedBatch && selectedJob
    ? applicationStage({ kind: "application", id: selectedBatch.id, batch: selectedBatch, job: selectedJob }) : null;
  const tabParam = params.get("tab");
  const requestedTab = tabParam === "waiting" ? "wait" : tabParam === "other" ? "applying" : tabParam;
  const active: ApplicationTab = TAB_IDS.find(id => id === requestedTab)
    ?? (selection ? selectedStage ? stageTab(selectedStage) : "all" : "wait");
  const counts: Record<ApplicationStage, number> = { applying: 0, waiting: 0, applied: 0, other: 0 };
  for (const item of items) { const stage = applicationStage(item); if (stage) counts[stage]++; }
  const total = counts.applying + counts.waiting + counts.applied + counts.other;
  const monitoringUnavailable = !!applications.data && applications.data.monitoring.state !== "available";
  const incomplete = applications.isError || monitoringUnavailable || (!!queue && (queue.state !== "available" || !!queue.error || !!queue.truncated));
  const partial = incomplete || !!applications.data?.monitoring.truncated || batches.some(batch => batch.jobs_truncated);
  const countLabel = (count: number) => applications.isPending || !applications.data ? "?" : partial ? count > 0 ? `${count}+` : "?" : String(count);
  const refresh = () => {
    void applications.refetch();
    void client.invalidateQueries({ queryKey: realJobsKeys.lists });
    void client.invalidateQueries({ queryKey: realJobsKeys.details });
  };
  const select = (next: RunSelection | null) => {
    const updated = new URLSearchParams(params);
    updated.set("tab", active);
    if (next) { updated.set("kind", next.kind); updated.set("run", next.id); }
    else { updated.delete("kind"); updated.delete("run"); }
    if (next?.kind === "application" && next.jobId !== undefined) updated.set("job", String(next.jobId));
    else updated.delete("job");
    setParams(updated);
    if (!next && selection) requestAnimationFrame(() => (document.getElementById(`run-select-${selectionKey(selection)}`) ?? document.getElementById("application-overview"))?.focus());
  };
  // Applications is one tab. Its stage filter keeps the earlier tab ids, so saved links to them still open the right rows.
  const stages: { id: ApplicationTab; label: string }[] = [
    { id: "all", label: `All (${countLabel(total)})` },
    { id: "applying", label: `Applying & other (${countLabel(counts.applying + counts.other)})` }, { id: "applied", label: "Applied" },
  ];
  const inApplications = stages.some(stage => stage.id === active);
  const tabs = [
    { id: "postings", label: "All postings" }, { id: "wait", label: "Wait" }, { id: "queue", label: "Application queue" },
    { id: "applications", label: `Applications (${countLabel(total)})` }, { id: "ignored", label: "Ignored" },
  ];
  const openTab = (id: string) => {
    const updated = new URLSearchParams(params);
    updated.set("tab", id === "applications" ? "all" : id);
    updated.delete("kind"); updated.delete("run"); updated.delete("job");
    setParams(updated);
  };
  const savedRows = (tab?: ApplicationTab, name?: string) => <CurrentRunsTable embedded title="Applications"
    items={tab ? items.filter(item => stageTab(applicationStage(item)) === tab) : items}
    loading={applications.isPending} incomplete={incomplete} selected={selection} onSelect={select}
    emptyText={tab ? `No saved jobs in ${name}.` : "No saved application runs or unassigned queued jobs to show."} />;
  const appliedRows = () => <SpaceBetween size="l">
      <section aria-label="Verified submissions"><Header variant="h3" counter={`(${countLabel(counts.applied)})`} description="Saved applications with verified submission evidence. Sync and tab closure are tracked separately.">Verified submissions</Header>{savedRows("applied", "Applied")}</section>
      <section aria-label="Manually marked Applied"><Header variant="h3" description="Your recorded Applied decisions. Marking a job Applied does not verify an employer submission.">Manually marked Applied</Header><TriageDecisions filter="applied" /></section>
    </SpaceBetween>;
  const stageRows = () => {
    if (active === "applied") return appliedRows();
    return active === "all" ? savedRows() : savedRows("applying", "Applying & other");
  };
  const content = () => {
    if (inApplications) return <Tabs variant="container" ariaLabel="Application stage" activeTabId={active} onChange={({ detail }) => openTab(detail.activeTabId)}
      tabs={stages.map(stage => ({ ...stage, content: stage.id === active ? stageRows() : null }))} />;
    if (active === "postings") return <LibraryPage />;
    if (active === "ignored") return <TriageDecisions filter="ignored" />;
    if (active === "queue") return applications.data ? <ApplicationQueueContent data={applications.data} /> : null;
    // Queued jobs live in the Application queue tab, so Wait only holds decisions not yet queued.
    if (active === "wait") return <section aria-label="Wait decisions"><Header variant="h3" description="Jobs you have shortlisted. Adding them to the queue moves them to the Application queue tab and does not start applications.">Wait decisions</Header><TriageDecisions filter="wait" /></section>;
    return null;
  };

  return <div data-testid="cloudscape-runs"><ContentLayout maxContentWidth={1280}><SpaceBetween size="l">
    <div className="workspace-toolbar">
      <Box color="text-body-secondary">Application observations refresh every 30 seconds. Last read {timestamp(applications.dataUpdatedAt)}.</Box>
      <SpaceBetween direction="horizontal" size="s">
        <RouteLink href="/runs?tab=queue">View application queue</RouteLink>
        <RouteLink href="/apply-health">Application diagnostics</RouteLink>
        <Button iconName="refresh" loading={applications.isFetching} onClick={refresh}>Refresh</Button>
      </SpaceBetween>
    </div>
    <ApplyReadStatus query={applications} />
    {monitoringUnavailable && <Alert type="info" header="Application observations unavailable">Current application progress is unknown. Application diagnostics has more information.</Alert>}
    {active !== "queue" && queue && queue.state !== "available" && <Alert type="info" header="Application queue unavailable">Wait application totals are unknown because stored queue records could not be read.</Alert>}
    {queue?.state === "available" && (queue.truncated || queue.error) && <Alert type="warning">Only a subset of stored queue jobs is available. The displayed counts may be incomplete.</Alert>}
    {applications.data?.monitoring.truncated && <Alert type="warning">Only a bounded selection of application runs is available.</Alert>}
    {batches.some(batch => batch.jobs_truncated) && <Alert type="warning">Only a bounded subset of observed application jobs is shown.</Alert>}
    {batches.length > 0 && <Box variant="small" color="text-body-secondary">Rerun application replaces the previous form and applier chat. The new applier uses the same resume and approved answers, then stops before Submit.</Box>}
    <ApplicationsOverview batches={batches}>
      <Tabs activeTabId={inApplications ? "applications" : active} onChange={({ detail }) => openTab(detail.activeTabId)}
        tabs={tabs.map(tab => ({ ...tab, content: tab.id === (inApplications ? "applications" : active) ? content() : null }))} />
    </ApplicationsOverview>
    {selection && <SelectedRunDetail selection={selection} batch={selectedBatch} applicationsLoading={applications.isPending} onClose={() => select(null)} onDone={refresh} />}
  </SpaceBetween></ContentLayout></div>;
}
