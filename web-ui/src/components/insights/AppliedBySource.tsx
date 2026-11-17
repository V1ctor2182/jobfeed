import BarChart from "@cloudscape-design/components/bar-chart";

import { CHART_I18N } from "@/components/insights/chartProps";
import { ChartCard, ChartEmpty } from "@/components/insights/ChartCard";
import { sourceLabel } from "@/components/runs/run-model";

/** Applied counts by source, largest first, labelled for people rather than by platform key. */
export function appliedSources(counts: Record<string, number>): { source: string; label: string; count: number }[] {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([source, count]) => ({ source, label: sourceLabel(source), count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

/** Applied jobs in the selected period, grouped by the source each was applied through. */
export function AppliedBySourceChart({ counts }: { counts: Record<string, number> }) {
  const rows = appliedSources(counts);
  if (rows.length === 0) {
    return <ChartCard title="Applied by source"><ChartEmpty>No applied jobs in this period.</ChartEmpty></ChartCard>;
  }
  const data = rows.map(({ label, count }) => ({ x: `${label} (${count.toLocaleString()})`, y: count }));
  return (
    <ChartCard title="Applied by source" description="Where each applied job's latest application came from.">
      <BarChart
        ariaLabel="Applied by source"
        ariaDescription="Applied job counts by source."
        i18nStrings={CHART_I18N}
        height={Math.max(150, rows.length * 32)}
        horizontalBars
        hideFilter
        hideLegend
        xScaleType="categorical"
        xDomain={data.map(({ x }) => x)}
        yTitle="Applied jobs"
        yTickFormatter={(value) => Math.round(value).toLocaleString()}
        series={[{ title: "Applied jobs", type: "bar", data }]}
      />
    </ChartCard>
  );
}
