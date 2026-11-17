import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useSyncExternalStore, type ReactNode } from "react";
import { formatLocalDateTime } from "@/lib/dates";
import { useDensity } from "@/lib/density";
import { label, LoadingSnapshot, RouteLink } from "@/components/workspace/shared";
import { ApplicationJobLinks } from "./ApplicationJobLinks";
import { applicationStage, applicationJobState, batchStatus, observationStatus, rowSelection, selectionKey, pipelineLabel, pipelineProgress, type CurrentRun, type StoredQueueApplication, type RunSelection } from "./run-model";
import { RunStatus } from "./RunHistoryTable";
import "./runs.css";

type RunTableItem = CurrentRun | StoredQueueApplication;

function rowKey(item: RunTableItem) {
  return item.kind === "queue" ? `queue-${item.job.job_id}` : selectionKey(rowSelection(item));
}

function subscribeToViewport(callback: () => void) {
  const query = window.matchMedia("(max-width: 600px)");
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

const isNarrowViewport = () => window.matchMedia("(max-width: 600px)").matches;

export function CurrentRunsTable({ items, loading, incomplete, selected, onSelect, history = false, saved = false, title: customTitle, description, actions, emptyText, embedded = false }: {
  items: RunTableItem[]; loading: boolean; incomplete?: boolean; selected: RunSelection | null;
  onSelect: (selection: RunSelection) => void; history?: boolean; saved?: boolean;
  title?: string; description?: ReactNode; actions?: ReactNode; emptyText?: string; embedded?: boolean;
}) {
  const { density } = useDensity();
  const narrow = useSyncExternalStore(subscribeToViewport, isNarrowViewport, () => false);
  const title = customTitle ?? (history ? "Completed application runs" : "Current runs");
  const loadingText = history ? "Loading completed application runs" : "Loading current runs";
  const header = <Header variant="h2" counter={`(${items.length})`} description={description} actions={actions}>{title}</Header>;
  const empty = <Box textAlign="center" padding="m">{incomplete ? "Some run information is unavailable. See the status above." : emptyText ?? (history ? "No completed application runs recorded." : "No current runs recorded.")}</Box>;
  const identity = (item: RunTableItem) => <RunIdentity item={item} selected={selected} onSelect={onSelect} />;
  const mobile = <>
    {items.length === 0 ? loading ? <LoadingSnapshot label={loadingText} /> : empty
      : <ul className="runs-mobile-list" aria-label={title}>
        {items.map(item => <li key={rowKey(item)}>
          <SpaceBetween size="xs">
            {identity(item)}
            <CurrentRunStatus item={item} saved={saved} />
            <Box>{progress(item)}</Box>
            <Box variant="small" color="text-body-secondary">{item.kind === "pipeline" ? "Started " : item.kind === "application" ? "Observed " : ""}{observedTime(item)}</Box>
          </SpaceBetween>
        </li>)}
      </ul>}
  </>;
  if (narrow) return embedded ? mobile : <Container header={header}>{mobile}</Container>;
  return <Table items={items} trackBy={rowKey} contentDensity={density}
    loading={loading && items.length === 0} loadingText={loadingText} skeleton={loading ? { totalRows: 3 } : undefined} wrapLines
    ariaLabels={{ tableLabel: title }} header={embedded ? undefined : header} variant={embedded ? "embedded" : "container"}
    columnDefinitions={[
      { id: "run", header: "Run", isRowHeader: true, minWidth: 200, cell: identity },
      { id: "status", header: "Status", minWidth: 180, cell: item => <CurrentRunStatus item={item} saved={saved} /> },
      { id: "progress", header: "Progress", minWidth: 220, cell: progress },
      { id: "time", header: "Started / observed", minWidth: 160, cell: observedTime },
    ]} empty={empty}
  />;
}

function RunIdentity({ item, selected, onSelect }: {
  item: RunTableItem; selected: RunSelection | null; onSelect: (selection: RunSelection) => void;
}) {
  if (item.kind === "queue") return <div data-testid={`current-run-queue-${item.job.job_id}`}>
    <Box variant="small" fontWeight="bold">Queued job</Box>
    <Box fontWeight="bold">{item.job.title || "Untitled application"}</Box>
    <Box variant="small" color="text-body-secondary">{item.job.company || "Company not recorded"}</Box>
    <div><RouteLink href="/runs?tab=queue">Open application queue</RouteLink></div>
  </div>;
  const choice = rowSelection(item);
  const key = selectionKey(choice);
  const title = item.kind === "pipeline" ? pipelineLabel(item.run.source) : item.job?.title || (item.job ? "Untitled application" : "Application batch");
  return <div data-testid={`current-run-${key}`}>
    {item.kind === "application" && item.job && <div><Box variant="small" fontWeight="bold">{applicationStage(item) === "waiting" ? "Waiting for applier" : item.job.applier_label ? `Applier ${item.job.applier_label}` : "Applier · label not recorded"}</Box></div>}
    <Button id={`run-select-${key}`} variant="inline-link" ariaLabel={item.kind === "application" && item.job ? `Open ${title} details` : `Open ${title} details`}
      ariaExpanded={selected !== null && selectionKey(selected) === key} ariaControls="selected-run-details"
      onClick={() => onSelect(choice)}>{title}</Button>
    {item.kind === "application" && item.job ? <>
      <div><Box variant="small" color="text-body-secondary">{item.job.company || "Company not recorded"}</Box></div>
      <ApplicationJobLinks batchId={item.batch.id} job={item.job} />
    </> : <div className="workspace-wrap"><Box variant="small" color="text-body-secondary">{item.kind === "pipeline" ? "Scan or evaluation" : "No bound job details available"}</Box></div>}
  </div>;
}

function CurrentRunStatus({ item, saved }: { item: RunTableItem; saved: boolean }) {
  if (item.kind === "queue") return <StatusIndicator type="pending">Wait</StatusIndicator>;
  return item.kind === "pipeline"
    ? saved ? <StatusIndicator type="warning">Saved status · refresh failed</StatusIndicator> : <RunStatus run={item.run.counters} compact />
    : <StatusIndicator type={item.batch.freshness === "fresh" && item.batch.source_binding === "valid" ? "info" : "warning"}>{item.job ? observationStatus(item.batch) : batchStatus(item.batch)}{item.job ? ` · ${applicationJobState(item.job)}` : ""}</StatusIndicator>;
}

function progress(item: RunTableItem) {
  if (item.kind === "queue") return "Queued · No application run recorded";
  if (item.kind === "pipeline") return pipelineProgress(item.run.counters);
  if (item.job?.submitted) return item.job.synced ? (item.job.closed ? "Submission, sync, and tab closure verified" : "Submission and sync verified · tab closure pending") : "Website submission verified · Jobfeed sync pending";
  if (item.job) return label(item.job.next_step || item.job.errors?.[0] || item.job.turn_state);
  const batch = item.batch;
  if (batch.source_binding !== "valid") return "Progress unavailable";
  const blocker = batch.blockers?.awaiting_user ? ` · ${batch.blockers.awaiting_user} needs input`
    : batch.blockers?.waiting_result ? ` · ${batch.blockers.waiting_result} waiting for result` : "";
  return `${batch.counts?.submitted ?? "?"}${batch.target == null ? "" : ` / ${batch.target}`} submitted · ${batch.counts?.occupied ?? "?"} occupied${blocker}`;
}

function observedTime(item: RunTableItem) {
  if (item.kind === "queue") return "Stored queue record · no run observation";
  return formatLocalDateTime(item.kind === "pipeline" ? item.run.started_at : item.batch.checked_at ?? null);
}
