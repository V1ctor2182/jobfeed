import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { useActiveRuns, useRuns } from "@/api/queries";
import { CurrentRunsTable } from "@/components/runs/CurrentRunsTable";
import { RunHistoryTable } from "@/components/runs/RunHistoryTable";
import { SelectedRunDetail } from "@/components/runs/SelectedRunDetail";
import { readSelection, selectionKey, type CurrentRun, type RunSelection } from "@/components/runs/run-model";
import { TriggerEvaluateButton } from "@/components/runs/TriggerEvaluateDialog";
import { TriggerScanButton } from "@/components/runs/TriggerScanDialog";

const PAGE_SIZE = 25;

export default function ScannerRunsPage() {
  const [page, setPage] = useState(0);
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const requested = readSelection(params);
  const selection = requested?.kind === "pipeline" ? requested : null;
  const runs = useRuns({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
  const active = useActiveRuns();
  const previousActive = useRef<string[]>([]);
  const refreshHistory = runs.refetch;
  useEffect(() => {
    if (!active.data || active.isError) return;
    const ids = active.data.runs.map(run => run.run_id);
    if (previousActive.current.some(id => !ids.includes(id))) void refreshHistory();
    previousActive.current = ids;
  }, [active.data, active.isError, refreshHistory]);
  const handleDone = useCallback(() => { void queryClient.invalidateQueries({ queryKey: ["runs"] }); }, [queryClient]);
  const select = (next: RunSelection | null) => {
    const updated = new URLSearchParams(params);
    if (next) { updated.set("kind", next.kind); updated.set("run", next.id); }
    else { updated.delete("kind"); updated.delete("run"); }
    updated.delete("job");
    setParams(updated);
    if (!next && selection) requestAnimationFrame(() => document.getElementById(`run-select-${selectionKey(selection)}`)?.focus());
  };
  const activeRuns = active.data?.runs ?? [];
  const activeIds = new Set(activeRuns.map(run => run.run_id));
  const historyRuns = runs.data?.runs.filter(run => !activeIds.has(run.run_id)) ?? [];
  const current: CurrentRun[] = activeRuns.map(run => ({ kind: "pipeline", id: run.run_id, run }));

  return <div data-testid="cloudscape-scanner-runs"><ContentLayout maxContentWidth={1280} header={
    <Header variant="h2" description="Discovery scans and job evaluations, with their own activity and history." actions={
      <SpaceBetween direction="horizontal" size="xs"><TriggerEvaluateButton /><TriggerScanButton /></SpaceBetween>
    }>Scanner activity</Header>
  }><SpaceBetween size="l">
    <div className="workspace-toolbar">
      <Box color="text-body-secondary">Scans discover jobs. Evaluations review discovered jobs.</Box>
      <Button iconName="refresh" loading={active.isFetching || runs.isFetching} onClick={handleDone}>Refresh</Button>
    </div>
    {active.isError && <Alert type="warning" header="Current pipeline runs unavailable">{active.data ? "Refresh failed. Showing saved pipeline records from the last successful read." : "Active scan and evaluation records could not be read."}</Alert>}
    <CurrentRunsTable title="Scans and evaluations" emptyText="No active scans or evaluations." items={current} loading={active.isPending}
      incomplete={active.isError} selected={selection} onSelect={select} saved={active.isError} />
    <ExpandableSection headerText="Scanner history" variant="container">
      <RunHistoryTable runs={historyRuns} total={runs.data?.total ?? 0} page={page} pageSize={PAGE_SIZE}
        isLoading={runs.isPending} isPlaceholder={runs.isPlaceholderData} error={runs.error} hasActiveRuns={activeIds.size > 0}
        expandedId={selection?.id ?? null} onExpandedChange={id => select(id ? { kind: "pipeline", id } : null)} onPageChange={setPage} />
    </ExpandableSection>
    {selection && <SelectedRunDetail asModal selection={selection} active={activeRuns.find(run => run.run_id === selection.id)}
      historical={historyRuns.find(run => run.run_id === selection.id)} applicationsLoading={false}
      onClose={() => select(null)} onDone={handleDone} />}
  </SpaceBetween></ContentLayout></div>;
}
