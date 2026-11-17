import { useApplyHealth } from "@/api/workspace";
import { ObservedJobs } from "@/components/workspace/ApplicationTable";
import { DashboardOverview } from "@/components/workspace/DashboardOverview";

/** Headline numbers once, then the current batch's jobs that need attention. Batch detail lives on Apply health. */
export default function DashboardPage() {
  const batch = useApplyHealth().data?.batches?.[0];
  return <>
    <DashboardOverview />
    {batch && <ObservedJobs key={batch.id} batch={batch} attentionOnly />}
  </>;
}
