import BarChart from "@cloudscape-design/components/bar-chart";

import { CHART_I18N } from "@/components/insights/chartProps";
import { ChartCard, ChartEmpty } from "@/components/insights/ChartCard";

/** Displays the four user decisions without exposing workflow status names. */
export function JobStatusChart({ distribution }: { distribution: Record<string, number> }) {
  const data = Object.entries(distribution)
    .filter(([, value]) => value > 0)
    .sort(([, left], [, right]) => right - left)
    .map(([status, value]) => ({ status: `${readableStatus(status)} (${value.toLocaleString()})`, value }));
  if (data.length === 0) {
    return <ChartCard title="User decisions"><ChartEmpty>No job decisions yet.</ChartEmpty></ChartCard>;
  }
  return (
    <ChartCard title="User decisions" description="Archived postings are included in Ignored.">
      <BarChart
        ariaLabel="User decisions"
        ariaDescription="Current job counts by user decision."
        i18nStrings={CHART_I18N}
        height={150}
        horizontalBars
        hideFilter
        hideLegend
        xScaleType="categorical"
        xDomain={data.map(({ status }) => status)}
        yTitle="Jobs"
        yTickFormatter={(value) => Math.round(value).toLocaleString()}
        series={[{ title: "Jobs", type: "bar", data: data.map(({ status, value }) => ({ x: status, y: value })) }]}
      />
    </ChartCard>
  );
}

function readableStatus(status: string): string {
  if (status === "results") return "Results";
  return status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
