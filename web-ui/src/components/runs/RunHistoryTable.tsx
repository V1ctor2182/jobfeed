import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import type { BadgeProps } from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";

import { type RunSummary, useRunDetail, useRunNewJobSources } from "@/api/queries";
import { formatLocalDateTime } from "@/lib/dates";
import { useDensity } from "@/lib/density";
import { isPartialScan, pipelineProgress, sourceLabel } from "./run-model";
import { RunNewJobsTable } from "./RunNewJobsTable";
import "./runs.css";

type CounterKey = Extract<
  keyof RunSummary,
  | "jobs_discovered"
  | "jobs_inserted"
  | "jobs_updated"
  | "jobs_filtered"
  | "jobs_ml_gated"
  | "jobs_seniority_filtered"
  | "jobs_scored"
  | "stage_a_scored"
  | "stage_b_scored"
  | "errors"
>;

const FULL_COUNTERS: {
  key: CounterKey;
  label: string;
  color: NonNullable<BadgeProps["color"]>;
}[] = [
  { key: "jobs_discovered", label: "discovered", color: "blue" },
  { key: "jobs_inserted", label: "new listings", color: "green" },
  { key: "jobs_updated", label: "updated", color: "grey" },
  {
    key: "jobs_filtered",
    label: "excluded by job rules",
    color: "severity-neutral",
  },
  {
    key: "jobs_ml_gated",
    label: "excluded by SDE role filter",
    color: "severity-neutral",
  },
  {
    key: "jobs_seniority_filtered",
    label: "excluded by seniority filter",
    color: "severity-neutral",
  },
  { key: "jobs_scored", label: "evaluated", color: "severity-low" },
  {
    key: "stage_a_scored",
    label: "quick evaluations",
    color: "severity-low",
  },
  {
    key: "stage_b_scored",
    label: "detailed reviews",
    color: "severity-medium",
  },
  { key: "errors", label: "errors", color: "red" },
];


interface RunHistoryTableProps {
  runs: RunSummary[];
  total: number;
  page: number;
  pageSize: number;
  isLoading: boolean;
  isPlaceholder: boolean;
  error: Error | null;
  hasActiveRuns: boolean;
  expandedId: string | null;
  onExpandedChange: (runId: string | null) => void;
  onPageChange: (page: number) => void;
}

export function RunHistoryTable(props: RunHistoryTableProps) {
  const { density } = useDensity();
  const pagesCount = Math.max(1, Math.ceil(props.total / props.pageSize));
  const toggle = (runId: string) => props.onExpandedChange(props.expandedId === runId ? null : runId);
  return (
    <div className="run-history" data-testid="run-history-density" data-density={density}>
      <SpaceBetween size="s">
      <Table
        items={props.runs}
        trackBy="run_id"
        // The Started button's own click (mouse or keyboard) bubbles here too, so
        // the whole row is one toggle target instead of only the date link.
        onRowClick={({ detail }) => toggle(detail.item.run_id)}
        loading={props.isLoading}
        loadingText="Loading runs"
        skeleton={{ totalRows: 6 }}
        contentDensity={density}
        ariaLabels={{ tableLabel: "Run history" }}
        header={
          <Header
            variant="h3"
            counter={`(${props.total})`}
            description="Choose a run to inspect its details below."
          >
            Scans and evaluations
          </Header>
        }
        columnDefinitions={[
          {
            id: "started",
            header: "Started",
            width: 270,
            isRowHeader: true,
            verticalAlign: "top",
            cell: (run) => <StartedCell run={run} isExpanded={props.expandedId === run.run_id} />,
          },
          { id: "source", header: "Run type", cell: (run) => runLabel(run.source) },
          { id: "status", header: "Status", cell: (run) => <RunStatus run={run} compact /> },
          {
            id: "activity",
            header: "Activity",
            cell: (run) => (
              <div data-testid={`run-activity-${run.run_id}`}><Activity run={run} /></div>
            ),
          },
          {
            id: "cost",
            header: "Cost",
            cell: (run) => run.total_llm_cost_usd > 0 ? formatCost(run.total_llm_cost_usd) : "—",
          },

        ]}
        pagination={
          <Pagination
            currentPageIndex={props.page + 1}
            pagesCount={pagesCount}
            pagesVariant="compact"
            disabled={props.isPlaceholder}
            ariaLabels={{
              paginationLabel: "Run history pages",
              previousPageLabel: "Previous page",
              nextPageLabel: "Next page",
              pageLabel: (page) => `Page ${page}`,
            }}
            onChange={({ detail }) => props.onPageChange(detail.currentPageIndex - 1)}
          />
        }
        empty={null}
      />
      {!props.isLoading && !props.error && props.runs.length === 0 && (
        <EmptyHistory hasActiveRuns={props.hasActiveRuns} />
      )}
      <RangeLabel
        page={props.page}
        pageSize={props.pageSize}
        pageRows={props.runs.length}
        total={props.total}
      />
      {props.error !== null && <Alert type="error" header="Run history could not be loaded">{props.error.message}{props.runs.length > 0 && " Showing saved, potentially stale run history from the last successful read."}</Alert>}
      </SpaceBetween>
    </div>
  );
}

function StartedCell({ run, isExpanded }: { run: RunSummary; isExpanded: boolean }) {
  return (
    <div id={`run-${run.run_id}`} data-testid={`run-row-${run.run_id}`}>
      <Button
        id={`run-select-pipeline-${run.run_id}`}
        variant="inline-link"
        iconName={isExpanded ? "angle-down" : "angle-right"}
        ariaLabel={`Run started ${formatLocalDateTime(run.started_at)}`}
        ariaExpanded={isExpanded}
      >
        {formatLocalDateTime(run.started_at)}
      </Button>

    </div>
  );
}

export function RunDetail({ run }: { run: RunSummary }) {
  const historical = useRunDetail(
    run.run_id,
    run.source === "evaluate" && run.verdict_counts == null && run.finished_at != null,
  );
  const verdicts = run.verdict_counts ?? historical.data?.verdict_counts;
  return (
    <Box margin={{ top: "s" }}>
      <SpaceBetween size="s">
        <ColumnLayout columns={3} variant="text-grid">
          {FULL_COUNTERS.map(({ key, label }) => (
            <div key={key}><Box variant="awsui-key-label">{label}</Box><Box>{run[key]}</Box></div>
          ))}
          {run.source === "evaluate" && (
            <>
              {(["apply", "consider", "skip"] as const).map((key) => (
                <div key={key} data-testid={`run-verdict-${key}`}>
                  <Box variant="awsui-key-label">{key === "skip" ? "Ignore" : key === "apply" ? "Apply" : "Consider"}</Box>
                  <Box>{verdicts?.[key] ?? "—"}</Box>
                </div>
              ))}
            </>
          )}
          <div><Box variant="awsui-key-label">finished</Box><Box>{formatLocalDateTime(run.finished_at)}</Box></div>
          {run.status === "failed" && (
            <>
              <div><Box variant="awsui-key-label">failure code</Box><Box>{run.failure_code ?? "—"}</Box></div>
              <div><Box variant="awsui-key-label">failed stage</Box><Box>{run.failed_stage ?? "—"}</Box></div>
              <div><Box variant="awsui-key-label">failed source</Box><Box>{run.failed_source ?? "—"}</Box></div>
              <div><Box variant="awsui-key-label">last progress</Box><Box>{formatLocalDateTime(run.last_progress_at ?? null)}</Box></div>
              {run.restarted_by_run_id && (
                <div>
                  <Box variant="awsui-key-label">replacement run</Box>
                  <Link href={`/scanner-runs?kind=pipeline&run=${encodeURIComponent(run.restarted_by_run_id)}`}>{run.restarted_by_run_id}</Link>
                </div>
              )}
            </>
          )}
        </ColumnLayout>
        {Object.entries(run.scan_progress ?? {}).map(([source, progress]) => (
          <div key={source}>
            <Box variant="awsui-key-label">{sourceLabel(source)}</Box>
            <StatusIndicator type={progress.phase === "failed" ? "error" : progress.phase === "completed_with_warnings" ? "warning" : "success"}>
              {progress.phase === "resolving" ? "deduplicating" : String(progress.phase ?? "unknown").replaceAll("_", " ")} · {progress.processed ?? 0} processed
            </StatusIndicator>
            {progress.message && <Box>{String(progress.message)}</Box>}
          </div>
        ))}
        {run.source !== "evaluate" && run.jobs_inserted > 0 && (
          <>
            <NewJobsBySource run={run} />
            <RunNewJobsTable runId={run.run_id} isRunning={run.finished_at == null} />
          </>
        )}
      </SpaceBetween>
    </Box>
  );
}

function NewJobsBySource({ run }: { run: RunSummary }) {
  const sources = useRunNewJobSources(run.run_id, true);
  const entries = Object.entries(sources.data?.source_counts ?? {}).sort(
    ([sourceA, countA], [sourceB, countB]) =>
      countB - countA || sourceA.localeCompare(sourceB),
  );
  return (
    <SpaceBetween size="xs">
      <Box variant="h3">New jobs by source</Box>
      {sources.isPending && <Box color="text-body-secondary">Loading source breakdown</Box>}
      {sources.error && <Box color="text-status-error">Source breakdown unavailable</Box>}
      {sources.data && entries.length === 0 && (
        <Box color="text-body-secondary">No source attribution recorded</Box>
      )}
      {entries.length > 0 && (
        <SpaceBetween direction="horizontal" size="xxs">
          {entries.map(([source, count]) => (
            <span key={source} data-testid={`run-source-${source}`}>
              <Badge color="green">{sourceLabel(source)} {count}</Badge>
            </span>
          ))}
        </SpaceBetween>
      )}
      {sources.data && (
        <Box color="text-body-secondary">
          {sources.data.total === run.jobs_inserted
            ? `${sources.data.total} total new listings`
            : `${sources.data.total} of ${run.jobs_inserted} new listings attributed`}
        </Box>
      )}
    </SpaceBetween>
  );
}

function Activity({ run }: { run: RunSummary }) {
  return <Box>{pipelineProgress(run)}{run.errors > 0 ? ` · ${run.errors} errors` : ""}</Box>;
}

export function RunStatus({ run, compact = false }: { run: RunSummary; compact?: boolean }) {
  if (isPartialScan(run)) {
    return <StatusIndicator type="warning">
      {compact ? "Partial" : `Partial scan: ${run.failure_message ?? "A source could not finish"}. ${run.jobs_inserted} new jobs are saved.`}
    </StatusIndicator>;
  }
  if (run.status === "failed") {
    return (
      <StatusIndicator type="error">
        {compact ? "Failed" : `Failed: ${run.failure_message ?? "Failure reason was not recorded"}`}
      </StatusIndicator>
    );
  }
  if (run.finished_at === null) return <StatusIndicator type="in-progress">Running</StatusIndicator>;
  if (run.errors > 0) return <StatusIndicator type="warning">Completed with errors</StatusIndicator>;
  if (Object.values(run.scan_progress ?? {}).some(progress => progress.phase === "completed_with_warnings")) {
    return <StatusIndicator type="warning">Completed with warnings</StatusIndicator>;
  }
  return <StatusIndicator type="success">Succeeded</StatusIndicator>;
}

function EmptyHistory({ hasActiveRuns }: { hasActiveRuns: boolean }) {
  return (
    <Box textAlign="center" color="inherit">
      <SpaceBetween size="xs">
        <b>{hasActiveRuns ? "First run in progress" : "No runs yet"}</b>
        <Box variant="p" color="text-body-secondary">
          {hasActiveRuns
            ? "Live counters appear above; completed evidence will land here."
            : "Use Start scan above to collect jobs."}
        </Box>
      </SpaceBetween>
    </Box>
  );
}

function RangeLabel({ page, pageSize, pageRows, total }: { page: number; pageSize: number; pageRows: number; total: number }) {
  if (total === 0) return <Box color="text-body-secondary">0 runs</Box>;
  if (pageRows === 0) return <Box color="text-body-secondary">No historical rows on this page · {total} runs total</Box>;
  return <Box color="text-body-secondary">{page * pageSize + 1}–{page * pageSize + pageRows} of {total}</Box>;
}

function formatCost(usd: number): string {
  return `$${usd.toFixed(usd > 0 && usd < 0.01 ? 4 : 2)}`;
}

function runLabel(source: string): string {
  if (source === "evaluate") return "Evaluation";
  if (source === "all") return "Scan · all sources";
  return `Scan · ${source}`;
}
