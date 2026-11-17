import { useCollection } from "@cloudscape-design/collection-hooks";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useState } from "react";

import { type RunNewJob, useRunNewJobs } from "@/api/queries";
import { DetailPane } from "@/components/jobs/DetailPane";
import { formatLocalDateTime } from "@/lib/dates";
import { useDensity } from "@/lib/density";
import { safeJobUrl, sourceLabel } from "./run-model";

const NO_JOBS: RunNewJob[] = [];

/** The postings one scan inserted for the first time, with job details on demand. */
export function RunNewJobsTable({ runId, isRunning = false }: { runId: string; isRunning?: boolean }) {
  const { density } = useDensity();
  const newJobs = useRunNewJobs(runId, true, isRunning);
  const [openJob, setOpenJob] = useState<RunNewJob | null>(null);
  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } =
    useCollection(newJobs.data?.jobs ?? NO_JOBS, {
      filtering: {
        fields: ["title", "company", "location"],
        noMatch: <Box textAlign="center" color="text-body-secondary">No postings match the filter</Box>,
      },
      pagination: { pageSize: 25 },
      sorting: { defaultState: { sortingColumn: { sortingField: "first_seen_at" } } },
    });
  const columnDefinitions: TableProps.ColumnDefinition<RunNewJob>[] = [
    {
      id: "title",
      header: "Title",
      isRowHeader: true,
      sortingField: "title",
      cell: (job) => (
        <Button variant="inline-link" ariaLabel={`Open ${job.company} ${job.title}`} onClick={() => setOpenJob(job)}>
          {job.title}
        </Button>
      ),
    },
    { id: "company", header: "Company", sortingField: "company", cell: (job) => job.company },
    { id: "location", header: "Location", cell: (job) => job.location || "—" },
    { id: "source", header: "Source", sortingField: "source", cell: (job) => sourceLabel(job.source) },
    { id: "first-seen", header: "First seen", sortingField: "first_seen_at", cell: (job) => formatLocalDateTime(job.first_seen_at) },
    { id: "posting", header: "Posting", cell: (job) => <PostingLink url={job.url} /> },
  ];
  return (
    <SpaceBetween size="xs">
      {newJobs.isPending && <Box color="text-body-secondary">Loading new postings</Box>}
      {newJobs.error && <Box color="text-status-error">New postings unavailable</Box>}
      {newJobs.data && newJobs.data.jobs.length === 0 && (
        <Box color="text-body-secondary">No new postings recorded</Box>
      )}
      {newJobs.data && newJobs.data.jobs.length > 0 && (
        <Table
          {...collectionProps}
          variant="embedded"
          items={items}
          trackBy="job_id"
          contentDensity={density}
          wrapLines={false}
          ariaLabels={{ tableLabel: "New postings from this scan" }}
          header={<Header variant="h3" counter={`(${newJobs.data.total})`}>New postings</Header>}
          filter={
            <TextFilter
              {...filterProps}
              filteringPlaceholder="Find by title, company, or location"
              filteringAriaLabel="Find new postings"
              countText={`${filteredItemsCount ?? 0} matches`}
            />
          }
          pagination={
            <Pagination
              {...paginationProps}
              ariaLabels={{
                paginationLabel: "New postings pages",
                previousPageLabel: "Previous page",
                nextPageLabel: "Next page",
                pageLabel: (page) => `Page ${page}`,
              }}
            />
          }
          columnDefinitions={columnDefinitions}
        />
      )}
      {newJobs.data?.is_truncated && (
        <Box color="text-body-secondary">
          Showing the first {newJobs.data.jobs.length} of {newJobs.data.total} new postings.
        </Box>
      )}
      {openJob !== null && (
        <Modal
          visible
          onDismiss={() => setOpenJob(null)}
          closeAriaLabel="Close job detail"
          header="Job scores and evidence"
          size="large"
          position="top"
        >
          <DetailPane jobId={openJob.job_id} />
        </Modal>
      )}
    </SpaceBetween>
  );
}

function PostingLink({ url }: { url: string }) {
  const safeUrl = safeJobUrl(url);
  return safeUrl
    ? <Link external href={safeUrl} externalIconAriaLabel="Opens job posting in a new tab">Open posting</Link>
    : <Box color="text-body-secondary">Unavailable</Box>;
}
