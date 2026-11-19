import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import { useMemo, useState } from "react";

import {
  useJobsList,
  type JobsQuery,
  type JobSummary,
} from "@/api/queries";
import { DetailPane } from "@/components/jobs/DetailPane";
import { LibraryTable } from "@/components/jobs/LibraryTable";
import { useDebouncedCallback } from "@/lib/use-debounced";

type LibrarySort = NonNullable<JobsQuery["sort"]>;

const PAGE_SIZE = 25;
const SEARCH_DEBOUNCE_MS = 250;

interface LibraryQueryState {
  sort: LibrarySort;
  search: string;
  page: number;
}

/**
 * Every posting ever seen, whatever was decided about it: a searchable
 * table with a detail drawer. Decisions and the queue have their own tabs
 * beside this one on the Jobs postings view.
 */
export default function LibraryPage() {
  const [state, setState] = useState<LibraryQueryState>({
    sort: "discovered_desc",
    search: "",
    page: 0,
  });
  const [searchInput, setSearchInput] = useState("");
  // Drawer target is a row snapshot (like triage's applyTarget): a
  // background refetch may drop the row from the page, but the open
  // drawer keeps showing the job the user clicked.
  const [drawerJob, setDrawerJob] = useState<JobSummary | null>(null);

  // The settled search commits together with its page reset in ONE state
  // update — no transient request with the new search and a stale offset.
  const commitSearch = useDebouncedCallback((search: string) => {
    setState((current) =>
      current.search === search ? current : { ...current, search, page: 0 },
    );
  }, SEARCH_DEBOUNCE_MS);

  /** A sort change resets the page: offsets don't survive it. */
  const applyFilter = (patch: Pick<LibraryQueryState, "sort">) => {
    setState((current) => ({ ...current, ...patch, page: 0 }));
  };


  // Library shows everything: no apply_hard_filters / dedupe /
  // require_verdict — those are triage-ingest concerns (plan A4).
  const query: JobsQuery = useMemo(
    () => ({
      tab: "all",
      canonical: true,
      sort: state.sort,
      search: state.search === "" ? undefined : state.search,
      limit: PAGE_SIZE,
      offset: state.page * PAGE_SIZE,
    }),
    [state],
  );
  const list = useJobsList(query, { keepPrevious: true });

  // Stranded page: a decide/restore can empty the trailing page (total
  // shrank under the current offset), leaving a settled empty response
  // while rows still exist. Clamp back to the last valid page via the
  // adjust-state-during-render pattern (react.dev: "storing information
  // from previous renders"). Placeholder payloads are skipped — their
  // total belongs to the previous query — and the page guard terminates
  // the loop: the clamped query returns rows, so the condition goes false.
  const settled = list.isPlaceholderData ? undefined : list.data;
  if (settled !== undefined && settled.jobs.length === 0 && settled.total > 0) {
    const lastPage = Math.max(0, Math.ceil(settled.total / PAGE_SIZE) - 1);
    if (state.page > lastPage) {
      setState({ ...state, page: lastPage });
    }
  }

  return (
    <div data-testid="cloudscape-library">
        <SpaceBetween size="m">
              <Input
                key="search"
                value={searchInput}
                onChange={({ detail }) => {
                  setSearchInput(detail.value);
                  commitSearch(detail.value);
                }}
                placeholder="Search company or title"
                ariaLabel="Search jobs"
                type="search"
              />
          <Container
            key="results"
            header={
              <Header
                variant="h2"
                description="Every saved posting, whatever you decided. Open one to review its scores and evidence."
                counter={list.data ? `(${list.data.total})` : undefined}
              >
                All postings
              </Header>
            }
            footer={
              <Pager
                page={state.page}
                pageRows={list.data?.jobs.length ?? 0}
                total={list.data?.total ?? 0}
                isPlaceholder={list.isPlaceholderData}
                onPage={(page) => setState((current) => ({ ...current, page }))}
              />
            }
          >
            <ListBody
              list={list}
              onOpen={setDrawerJob}
              sort={state.sort}
              onSort={(sort) => applyFilter({ sort })}
            />
          </Container>
        </SpaceBetween>
      {drawerJob !== null && (
        <Modal
          visible
          onDismiss={() => setDrawerJob(null)}
          closeAriaLabel="Close job detail"
          header="Job scores and evidence"
          size="large"
          position="top"
        >
          <DetailPane
            jobId={drawerJob.id}
          />
        </Modal>
      )}
    </div>
  );
}

interface ListBodyProps {
  list: ReturnType<typeof useJobsList>;
  onOpen: (job: JobSummary) => void;
  sort: LibrarySort;
  onSort: (sort: LibrarySort) => void;
}

function ListBody({ list, onOpen, sort, onSort }: ListBodyProps) {
  if (list.isPending) {
    return (
      <Box textAlign="center" padding="l"><Spinner size="large" /></Box>
    );
  }
  if (list.isError) {
    return <Alert type="error">{list.error.message}</Alert>;
  }
  const jobs = list.data.jobs;
  if (jobs.length === 0) {
    return (
      <Box textAlign="center" padding={{ vertical: "xxl", horizontal: "l" }}>
        <SpaceBetween size="xxs">
          <Box variant="h3">Nothing here</Box>
          <Box color="text-body-secondary">
            Every posting ever seen lands in Library — try another tab or clear the search.
          </Box>
        </SpaceBetween>
      </Box>
    );
  }
  return <LibraryTable jobs={jobs} onOpen={onOpen} sort={sort} onSort={onSort} />;
}

interface PagerProps {
  page: number;
  pageRows: number;
  total: number;
  /** keepPreviousData is showing the previous query's page — its total
   * is stale, so Next must not page against it. */
  isPlaceholder: boolean;
  onPage: (page: number) => void;
}

/** Server-side Cloudscape pagination with an exact visible range. */
function Pager({ page, pageRows, total, isPlaceholder, onPage }: PagerProps) {
  const start = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const end = page * PAGE_SIZE + pageRows;
  return (
    <SpaceBetween direction="horizontal" size="m" alignItems="center">
      <Box variant="small" color="text-body-secondary">
        {total === 0 ? "0 results" : `${start}–${end} of ${total}`}
      </Box>
      <Pagination
        currentPageIndex={page + 1}
        pagesCount={Math.max(1, Math.ceil(total / PAGE_SIZE))}
        disabled={isPlaceholder}
        ariaLabels={{
          paginationLabel: "Library pagination",
          previousPageLabel: "Previous page",
          nextPageLabel: "Next page",
          pageLabel: (pageNumber) => `Page ${pageNumber}`,
        }}
        onChange={({ detail }) => onPage(detail.currentPageIndex - 1)}
      />
    </SpaceBetween>
  );
}
