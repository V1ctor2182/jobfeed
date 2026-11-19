import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useReadiness, type ReadinessCheck as Check } from "@/api/readiness";
import { timestamp } from "./shared";
import "./ReadinessCheck.css";

const statuses = {
  ready: { type: "success", label: "Ready" },
  blocked: { type: "error", label: "Blocked" },
  not_tested: { type: "pending", label: "Not tested" },
} as const;

export function ReadinessCheck({ compact = false }: { compact?: boolean }) {
  const { query, run, busy, unresolved, start } = useReadiness();
  return <Container header={<Header variant="h2" description="Check the application prerequisites before starting a batch." actions={
    <Button variant="primary" disabled={busy || !query.data?.csrf_token || query.isError} loading={busy && !unresolved && run?.phase !== "dispatch_unknown" && run?.phase !== "cleanup_unknown"} onClick={() => { void start(); }}>Run readiness check</Button>
  }>Application readiness</Header>}>
    <SpaceBetween size="m">
      <Box>This check uses the Codex harness with a local test page and a synthetic PDF to verify the Chrome extension connection and synthetic file selection. It does not start an application. Employer logins, file acceptance, and individual job forms still need checking during each job.</Box>
      {query.isPending && <div role="status">Loading the last readiness check.</div>}
      {query.isError && <Alert type="warning" header="Readiness status unavailable">The latest status could not be read. Saved results may be stale. <Button onClick={() => { void query.refetch(); }}>Refresh readiness status</Button></Alert>}
      {unresolved && <Alert type="warning" header="Dispatch outcome unknown">The request may have reached Codex. Checking for its result. A second check is disabled to avoid duplicate chats.</Alert>}
      {run && <>
        <div role="status" aria-live="polite">
          <StatusIndicator type={unresolved || query.isError ? "pending" : run.stale ? "warning" : run.phase === "running" ? "in-progress" : run.phase === "dispatch_unknown" || run.phase === "cleanup_unknown" ? "warning" : run.ready ? "success" : "error"}>
            {unresolved || query.isError ? "Previous readiness result" : run.stale ? "Readiness result is stale" : run.phase === "running" ? "Readiness check running" : run.phase === "dispatch_unknown" ? "Dispatch outcome unknown" : run.phase === "cleanup_unknown" ? "Test cleanup unconfirmed" : run.ready ? "Required components ready" : "Readiness blocked"}
          </StatusIndicator>
        </div>
        <Box variant="small" color="text-body-secondary">Started {timestamp(run.started_at)} · Finished {timestamp(run.finished_at)} · Expires {timestamp(run.expires_at)}</Box>
        {run.stale && <Alert type="warning">Run a new check before relying on these results.</Alert>}
        {(run.phase === "dispatch_unknown" || run.phase === "cleanup_unknown") && <Alert type="warning">Review the readiness chat and confirm its test tab is closed. Another run is disabled while the outcome is unresolved.</Alert>}
        {compact && run.checks.filter(check => check.required && check.status === "blocked").map(check => <Box key={check.id}><strong>{check.label}.</strong> {check.reason}</Box>)}
        <ExpandableSection headerText="Diagnostic details" defaultExpanded={!compact}>
        <Table<Check> ariaLabels={{ tableLabel: "Application readiness components" }} trackBy="id" items={run.checks} wrapLines variant="embedded" columnDefinitions={[
          { id: "component", header: "Component", cell: (item) => <>{item.label}<Box variant="small" display="block">{item.required ? "Required" : "Per batch or job"}</Box></> },
          { id: "status", header: "Status", width: 150, minWidth: 150, cell: (item) => <StatusIndicator type={statuses[item.status].type}>{statuses[item.status].label}</StatusIndicator> },
          { id: "reason", header: "Result", cell: (item) => item.reason },
          { id: "next", header: "Next step", width: 300, minWidth: 200, cell: (item) => <div className="readiness-next-step">{item.next_step || (item.status === "ready" ? "No action needed." : item.required || item.status === "blocked" ? "Follow result instructions, then run the check again." : "Check when the batch or job runs.")}</div> },
          { id: "checked", header: "Checked", cell: (item) => timestamp(item.checked_at) },
        ]} empty={<Box>No component results have been recorded yet.</Box>} />
        </ExpandableSection>
      </>}
      {query.isSuccess && !run && <Box>No readiness check has run. Start one to see local prerequisites and the browser connection result.</Box>}
    </SpaceBetween>
  </Container>;
}
