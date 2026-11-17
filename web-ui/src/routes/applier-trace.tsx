import { useState } from "react";
import { useSearchParams } from "react-router";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useApplierRuns, useApplierTrace, type ApplierRun } from "@/api/queries";
import { SAMPLE_APPLIER_TRACE } from "@/components/trace/sample-applier-trace";
import { TraceWaterfall } from "@/components/trace/TraceWaterfall";

function runOption(run: ApplierRun): SelectProps.Option {
  const status = run.is_live ? "Live" : run.state.charAt(0).toUpperCase() + run.state.slice(1);
  return {
    value: String(run.job_id),
    label: [run.company, run.title, `job #${run.job_id}`, status].filter(Boolean).join(" · "),
  };
}

/** Shows recorded Claude Applier runs: a live run as it happens, a finished one fully drawn. */
export default function ApplierTracePage() {
  const runs = useApplierRuns();
  const [params, setParams] = useSearchParams();
  const list = runs.data?.runs ?? [];
  const requested = Number(params.get("job"));
  const selected = list.find((run) => run.job_id === requested) ?? list[0] ?? null;
  const trace = useApplierTrace(selected?.job_id ?? null);

  if (runs.isPending) return <StatusIndicator type="loading">Loading Applier runs</StatusIndicator>;
  if (runs.isError) return <Alert type="error" header="Applier runs unavailable">{runs.error.message}</Alert>;
  if (!selected) return <NoRuns />;
  return <SpaceBetween size="l">
    <FormField label="Applier run">
      <Select
        selectedOption={runOption(selected)}
        options={list.map(runOption)}
        onChange={({ detail }) => setParams({ job: detail.selectedOption.value ?? "" })}
      />
    </FormField>
    {trace.isError && <Alert type="error" header="Trace unavailable">{trace.error.message}</Alert>}
    {trace.data
      ? <TraceWaterfall
          key={`${trace.data.job_id}:${trace.data.is_live}`}
          trace={trace.data}
          live={trace.data.is_live}
          initialNow={trace.data.root.end}
          autoPlay={false}
        />
      : !trace.isError && <StatusIndicator type="loading">Loading trace</StatusIndicator>}
  </SpaceBetween>;
}

function NoRuns() {
  const [preview, setPreview] = useState(false);
  if (preview) {
    return <SpaceBetween size="l">
      <Alert type="info" header="Sample run" action={<Button onClick={() => setPreview(false)}>Close preview</Button>}>
        This is a made-up run with made-up timings. Real Applier runs show here once a worker starts one.
      </Alert>
      <TraceWaterfall trace={SAMPLE_APPLIER_TRACE} />
    </SpaceBetween>;
  }
  return <Container>
    <Box textAlign="center" padding="l">
      <SpaceBetween size="s">
        <Box variant="h3">No Applier has run yet</Box>
        <Box color="text-body-secondary">A Claude Applier run shows here once a worker starts one.</Box>
        <Box><Button onClick={() => setPreview(true)}>Preview a sample run</Button></Box>
      </SpaceBetween>
    </Box>
  </Container>;
}
