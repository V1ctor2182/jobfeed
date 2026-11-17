import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import Pagination from "@cloudscape-design/components/pagination";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";

import {
  useRealJobsList,
  useRealJobTransition,
  fetchAllMatchingRealJobIds,
  realJobsKeys,
  type RealJobsListResponse,
  type RealJobsQuery,
} from "@/api/queries";
import { BulkBar } from "@/components/jobs/BulkBar";
import { DetailPane, type UserDecision } from "@/components/jobs/DetailPane";
import { JobList } from "@/components/jobs/JobList";
import { toast } from "@/components/ui/use-toast";
import { useSelection } from "@/lib/use-selection";
import { useApplicationQueueAdmission } from "@/api/application-queue";
import { RouteLink } from "@/components/workspace/shared";

export type TriageFilter = "wait" | "applied" | "ignored";
type TriageSort = "posted_asc" | "posted_desc" | "score_asc" | "score_desc";
type TriageJob = RealJobsListResponse["jobs"][number];

const PAGE_LIMIT = 25;

/** Triage query with server-side pagination and user-selected ordering. */
function triageQuery(filter: TriageFilter, page: number, sort: TriageSort): RealJobsQuery {
  return {
    decision: filter,
    sort: `triage_${sort}`,
    limit: PAGE_LIMIT,
    offset: page * PAGE_LIMIT,
  };
}

export function TriageDecisions({ filter }: { filter: TriageFilter }) {
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<TriageSort>("posted_desc");
  const [isSelectingAll, setIsSelectingAll] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [optimisticDecision, setOptimisticDecision] = useState<{
    id: string;
    filter: TriageFilter;
  } | null>(null);
  const selection = useSelection();
  const queryClient = useQueryClient();
  const queueAdmission = useApplicationQueueAdmission(filter === "wait");
  const [queueError, setQueueError] = useState<string | null>(null);
  const deselectQueued = selection.deselectMany;
  useEffect(() => {
    if (!queueAdmission.result || filter !== "wait") return;
    deselectQueued(queueAdmission.result.items.filter(item => item.status !== "rejected").map(item => item.real_job_id).filter((id): id is string => id !== null));
  }, [queueAdmission.result, deselectQueued, filter]);
  const addToQueue = async () => {
    setQueueError(null);
    try { await queueAdmission.submit(selection.selectedIds); }
    catch (error) { setQueueError(error instanceof Error ? error.message : "The queue request could not be confirmed."); }
  };

  const query = useMemo(() => triageQuery(filter, page, sort), [filter, page, sort]);
  const list = useRealJobsList(query);
  const rawJobs = useMemo(() => list.data?.jobs ?? [], [list.data]);
  const hidesCurrentRow = optimisticDecision?.filter === filter
    && rawJobs.some((job) => job.id === optimisticDecision.id);
  const jobs = useMemo(
    () => hidesCurrentRow
      ? rawJobs.filter((job) => job.id !== optimisticDecision?.id)
      : rawJobs,
    [hidesCurrentRow, optimisticDecision?.id, rawJobs],
  );
  const displayedTotal = Math.max(0, (list.data?.total ?? 0) - (hidesCurrentRow ? 1 : 0));
  const totalIsExact = list.data?.total_is_exact !== false;
  // Decide/advance handlers read the displayed order through a ref so a
  // setTimeout never closes over a stale list. Synced in an effect (refs
  // must not be written during render); handlers only run after effects.
  const rowsRef = useRef<TriageJob[]>(jobs);
  useEffect(() => {
    rowsRef.current = jobs;
  });

  const transition = useRealJobTransition();

  // Selection is derived, not effect-seeded: when nothing is selected or
  // the selected id dropped out of a refetched list, fall back to the
  // first row (the legacy seed + onBulkCleared re-seed semantics).
  const effectiveSelectedId =
    selectedId !== null && jobs.some((job) => job.id === selectedId)
      ? selectedId
      : (jobs[0]?.id ?? null);
  const selectedJob = jobs.find((job) => job.id === effectiveSelectedId) ?? null;

  /** Move selection to the next row in displayed order (previous when
   * the decided row was last; cleared when it was alone). Decisions only
   * ever target the effective selected row, so the set is unconditional. */
  const advanceFrom = (id: string) => {
    const rows = rowsRef.current;
    const index = rows.findIndex((job) => job.id === id);
    const next = index === -1 ? undefined : (rows[index + 1] ?? rows[index - 1]);
    setSelectedId(next?.id ?? null);
  };

  /** Remove completed decisions from both the provisional and exact current
   * page caches. The authoritative refetch still runs in the background to
   * refill the page and reconcile counts/dedupe. */
  const removeCompletedFromCurrentPage = (completedIds: string[]) => {
    const completed = new Set(completedIds);
    queryClient.setQueriesData<RealJobsListResponse>(
      { queryKey: realJobsKeys.list(query) },
      (previous) => {
        if (previous === undefined) {
          return previous;
        }
        const removedCount = previous.jobs.filter((job) => completed.has(job.id)).length;
        if (removedCount === 0) {
          return previous;
        }
        return {
          ...previous,
          jobs: previous.jobs.filter((job) => !completed.has(job.id)),
          total: Math.max(0, previous.total - removedCount),
        };
      },
    );
  };

  const decide = (to: UserDecision) => {
    const id = effectiveSelectedId;
    if (id === null || transition.isPending || queueAdmission.attempt?.state === "sending") {
      return;
    }
    const sourceFilter = filter;
    advanceFrom(id);
    setOptimisticDecision({ id, filter: sourceFilter });
    transition.mutate(
      { id, to },
      {
        onSuccess: () => {
          removeCompletedFromCurrentPage([id]);
          setOptimisticDecision(null);
        },
        onError: (error) => {
          setOptimisticDecision(null);
          setSelectedId(id);
          toast({
            variant: "destructive",
            title: "Decision could not be saved",
            description: error.message,
          });
        },
      },
    );
  };

  const onBulkResult = (failedIds: string[]) => {
    const failed = new Set(failedIds);
    const completedIds = selection.selectedIds.filter((id) => !failed.has(id));
    selection.deselectMany(completedIds);
    if (completedIds.includes(selectedId ?? "")) {
      setSelectedId(null);
    }
    removeCompletedFromCurrentPage(completedIds);
  };

  const selectAllMatching = async () => {
    if (list.data === undefined || !totalIsExact || isSelectingAll) {
      return;
    }
    setIsSelectingAll(true);
    try {
      const snapshot = await fetchAllMatchingRealJobIds(query);
      const ids = [...new Set(snapshot.real_job_ids)];
      if (ids.length !== snapshot.total) {
        throw new Error("Selection count did not match the server snapshot");
      }
      selection.selectAll(ids);
    } catch (error) {
      toast({
        variant: "destructive",
        title: "Jobs could not be selected",
        description: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setIsSelectingAll(false);
    }
  };

  return (
    <Grid
      gridDefinition={[
        { colspan: { default: 12, s: 7 } },
        { colspan: { default: 12, s: 5 } },
      ]}
    >
      <div data-testid="triage-decision-surface">
        <SpaceBetween size="s">
          <Box variant="small" color="text-body-secondary">
            {list.data !== undefined ? `${displayedTotal}${totalIsExact ? "" : "+"} jobs` : "Loading"}
          </Box>
          {triageListContent()}
        </SpaceBetween>
      </div>
      <section aria-label="Job detail">
        <Container
          header={
            <Header variant="h2" description="Review the recommendation, evidence, and your decision.">
              {selectedJob ? `${selectedJob.company} · ${selectedJob.title}` : "Job evidence"}
            </Header>
          }
        >
          <DetailPane
            jobId={effectiveSelectedId}
            realJob
            decisionView={filter}
            isDeciding={transition.isPending || queueAdmission.attempt?.state === "sending"}
            onDecide={decide}
            emptyHint="Select a result to review its evidence."
          />
        </Container>
      </section>
    </Grid>
  );

  function triageListContent() {
    return (
      <SpaceBetween size="xs">
        <BulkBar
          canonical
          currentDecision={filter}
          selectedIds={selection.selectedIds}
          total={displayedTotal}
          totalIsExact={totalIsExact}
          onSelectPage={() => selection.selectMany(jobs.map((job) => job.id))}
          isSelectingAll={isSelectingAll}
          onSelectAllMatching={() => void selectAllMatching()}
          onClear={selection.clear}
          onBulkResult={onBulkResult}
          actionPending={queueAdmission.attempt?.state === "sending"}
          additionalActions={filter === "wait" ? bulkPending => <Button variant="primary"
            loading={queueAdmission.attempt?.state === "sending"}
            disabled={Boolean(queueAdmission.attempt) || !queueAdmission.status.data?.csrf_token || queueAdmission.status.isError || isSelectingAll || transition.isPending || bulkPending || selection.count > 100}
            onClick={() => { void addToQueue(); }}>Add to application queue</Button> : undefined}
        />
        {filter === "wait" && <SpaceBetween size="s">
          {selection.count > 100 && <Alert type="info">Choose up to 100 jobs for one queue request. Clear this selection, then select a smaller group or individual pages.</Alert>}
          <Box variant="small">Queued jobs move to the Application queue tab, where you can select them and click Apply. Adding jobs does not start applications.</Box>
          {queueAdmission.attempt?.state === "uncertain" && <Alert type="warning" header="Queue request delivery unconfirmed"
            action={<Button onClick={() => { void queueAdmission.status.refetch(); }}>Check queue status</Button>}>
            No duplicate request will be sent. Check the saved request before adding more jobs. <RouteLink href="/runs?tab=queue">View application queue</RouteLink>
          </Alert>}
          {queueError && !queueAdmission.attempt && <Alert type="error" header="Jobs could not be queued">{queueError}</Alert>}
          {queueAdmission.status.isError && <Alert type="warning" header="Queue status unavailable"
            action={<Button onClick={() => { void queueAdmission.status.refetch(); }}>Check queue status</Button>}>The queue must be readable before jobs can be added.</Alert>}
          {queueAdmission.result && <Alert type={queueAdmission.result.items.some(item => item.status === "rejected") ? "warning" : "success"}
            header={queueAdmission.result.items.some(item => item.status === "rejected") ? "Some jobs were not queued" : "Selected jobs are in the application queue"}>
            <SpaceBetween size="xs">
              <Box>Queued jobs moved to the Application queue tab. No applications were started.</Box>
              {queueAdmission.result.items.filter(item => item.status === "rejected").map(item => <Box key={item.real_job_id}>Job {item.real_job_id}: {item.reason || "Not eligible for the application queue."}</Box>)}
              <RouteLink href="/runs?tab=queue">View application queue</RouteLink>
            </SpaceBetween>
          </Alert>}
        </SpaceBetween>}
        <ListBody
          filter={filter}
          list={list}
          jobs={jobs}
          isChecked={selection.isSelected}
          onOpen={setSelectedId}
          onToggle={selection.toggle}
          sort={sort}
          onSort={(nextSort) => {
            setSort(nextSort);
            setPage(0);
            setSelectedId(null);
          }}
        />
        {totalIsExact && displayedTotal > PAGE_LIMIT && (
          <SpaceBetween size="xs" alignItems="center">
            <Pagination
              currentPageIndex={page + 1}
              pagesCount={Math.ceil(displayedTotal / PAGE_LIMIT)}
              disabled={list.isFetching}
              ariaLabels={{
                paginationLabel: "Results pagination",
                previousPageLabel: "Previous page",
                nextPageLabel: "Next page",
                pageLabel: (pageNumber) => `Page ${pageNumber}`,
              }}
              onChange={({ detail }) => {
                setPage(detail.currentPageIndex - 1);
                setSelectedId(null);
              }}
            />
          </SpaceBetween>
        )}
      </SpaceBetween>
    );
  }
}

interface ListBodyProps {
  filter: TriageFilter;
  list: ReturnType<typeof useRealJobsList>;
  jobs: TriageJob[];
  isChecked: (id: string) => boolean;
  onOpen: (id: string) => void;
  onToggle: (id: string) => void;
  sort: TriageSort;
  onSort: (sort: TriageSort) => void;
}

function ListBody({ filter, list, jobs, ...rowProps }: ListBodyProps) {
  if (list.isPending) {
    return (
      <Box textAlign="center" padding="l">
        <Spinner size="large" />
      </Box>
    );
  }
  if (list.isError) {
    return <Alert type="error">{list.error.message}</Alert>;
  }
  if (jobs.length === 0) {
    return (
      <Box textAlign="center" padding="l">
        <SpaceBetween size="xs">
          <Box variant="strong">No {filter} jobs</Box>
          <Box color="text-body-secondary">Change the filter to see another decision.</Box>
        </SpaceBetween>
      </Box>
    );
  }
  return <JobList jobs={jobs} {...rowProps} />;
}
