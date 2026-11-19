import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useEffect, useRef } from "react";
import { useRunDetail, type ActiveRunEntry, type RunSummary } from "@/api/queries";
import type { BatchHealth } from "@/api/workspace";
import { ApplicationRunDetail } from "./ApplicationRunDetail";
import { LiveRunRow } from "./LiveRunRow";
import { RunActionButton } from "./RunActionButton";
import { RunDetail, RunStatus } from "./RunHistoryTable";
import { pipelineLabel, selectionKey, type RunSelection } from "./run-model";

export function SelectedRunDetail({ selection, active, historical, batch, applicationsLoading, onClose, onDone, asModal = false }: {
  selection: RunSelection; active?: ActiveRunEntry; historical?: RunSummary; batch?: BatchHealth;
  applicationsLoading: boolean; onClose: () => void; onDone: () => void;
  /** Show the details in a dialog over the page instead of inline below the tables. */
  asModal?: boolean;
}) {
  const query = useRunDetail(selection.id, selection.kind === "pipeline" && !active && !historical);
  const run = historical ?? query.data;
  const jobId = selection.kind === "application" ? selection.jobId : undefined;
  const job = batch?.source_binding === "valid" ? batch.jobs?.find(item => item.job_id === jobId) : undefined;
  const selectedKey = selectionKey(selection);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (!asModal) ref.current?.focus(); }, [selectedKey, asModal]);
  const title = selection.kind === "application" ? job?.title || "Application run details" : `${pipelineLabel(active?.source ?? run?.source ?? "run")} details`;
  const description = <span className="workspace-wrap">{selection.kind === "pipeline" ? selection.id : job?.company || "Saved application observation"}</span>;
  const body = <>
      {selection.kind === "pipeline" && !active && !historical && query.isError && query.data && <Alert type="warning" header="Run details refresh failed">Showing saved, potentially stale run details from the last successful read.</Alert>}
      {selection.kind === "application" ? batch ? <ApplicationRunDetail key={selectedKey} batch={batch} jobId={jobId} />
        : applicationsLoading ? <Box>Loading application run</Box> : <Alert type="warning">This application run is unavailable in the current saved observations.</Alert>
        : active ? <ul aria-label="Selected live run"><LiveRunRow key={active.run_id} run={active} onDone={onDone} /></ul>
          : run ? <SpaceBetween size="m">
            <SpaceBetween direction="horizontal" size="s">
              <RunStatus run={run} />
              {run.status === "running" && run.finished_at === null
                ? <RunActionButton runId={run.run_id} isRunning />
                : ((run.status === "failed" && !run.restarted_by_run_id) || (run.source === "evaluate" && run.errors > 0))
                  && <RunActionButton runId={run.run_id} isRunning={false} />}
            </SpaceBetween>
            <RunDetail run={run} />
          </SpaceBetween>
            : query.isPending ? <Box>Loading run details</Box>
              : <Alert type="warning">This run is unavailable. It may no longer exist or its details could not be read.</Alert>}
  </>;
  if (asModal) return <Modal visible onDismiss={onClose} closeAriaLabel="Close run details" size="max" position="top"
    header={<Header variant="h2" description={description}>{title}</Header>}>
    <section id="selected-run-details" aria-label="Selected run details">{body}</section>
  </Modal>;
  return <section id="selected-run-details" aria-label="Selected run details" ref={ref} tabIndex={-1}>
    <Container header={<Header variant="h2" description={description} actions={<Button onClick={onClose}>Close run details</Button>}>{title}</Header>}>
      {body}
    </Container>
  </section>;
}
