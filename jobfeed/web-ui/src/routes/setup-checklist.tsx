import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useNavigate } from "react-router";
import { useSetupChecklist, type SetupSection } from "@/api/checklist";
import { ReadinessCheck } from "@/components/workspace/ReadinessCheck";
import "./setup-checklist.css";

const status = {
  ready: { type: "success", text: "Ready" },
  needs_action: { type: "warning", text: "Needs attention" },
  not_tested: { type: "pending", text: "Not verified" },
} as const;

export default function SetupChecklistPage() {
  const navigate = useNavigate();
  const query = useSetupChecklist();
  return <div className="setup-checklist"><ContentLayout maxContentWidth={1200} header={
    <Header variant="h1" description="Set up each part of your job application workspace. Progress comes from the current local configuration." actions={
      <SpaceBetween direction="horizontal" size="xs">
        <Button onClick={() => navigate("/dashboard")}>Dashboard</Button>
        <Button variant="primary" onClick={() => navigate("/profile")}>My Profile</Button>
        <Button onClick={() => navigate("/setup/settings")}>Advanced settings</Button>
        <Button loading={query.isFetching} onClick={() => { void query.refetch(); }}>Refresh checklist</Button>
      </SpaceBetween>
    }>Workspace setup</Header>
  }>
    <SpaceBetween size="l">
      <Alert type="info" header="Setup does not start applications">
        Save your settings and verify the connection here. Importing URLs, scanning jobs, and starting an application batch are separate actions. Each batch still needs your job scope and submission instructions.
      </Alert>
      {query.isPending && <div role="status">Checking your workspace setup.</div>}
      {query.isError && <Alert type="error" header="Setup status could not be loaded">Check that the local dashboard is running, then refresh the checklist. Readiness is unknown until the current settings can be read.</Alert>}
      {query.data && query.isError && <Alert type="warning">The checklist below is from the last successful read.</Alert>}
      {query.data?.sections.map(section => <ChecklistSection key={section.id} section={section} />)}
      <Container header={<Header variant="h2">Add your first URLs with Codex or Claude Code</Header>}>
        <SpaceBetween size="m">
          <Box>Open a local Codex or Claude Code chat in your Jobfeed folder. Reload the client to load the project connection, enable the Jobfeed MCP, and ask it to check Jobfeed capabilities before pasting your job URLs.</Box>
          <blockquote className="setup-example">Use the jobfeed MCP to add these job URLs to the application queue as my priorities. Split lists into batches of at most 20. Report newly queued jobs, duplicates, and failures. Do not start applications.</blockquote>
          <Box>Use exact job URLs, one or several at a time. Job or company names need to be resolved to job URLs first. Jobs you add receive user priority. Existing URLs keep their queue state. Related postings on different application portals are flagged. Imported jobs appear in Apply health; the dashboard refreshes every 30 seconds.</Box>
          <Button onClick={() => navigate("/apply-health")}>View application queue</Button>
        </SpaceBetween>
      </Container>
      <section id="application-readiness" aria-label="Application readiness check"><ReadinessCheck compact /></section>
      <Box variant="small" color="text-body-secondary">A successful local diagnostic verifies the browser connection and synthetic file selection. Employer logins, missing personal answers, and each site's form requirements are checked when you authorize a job.</Box>
    </SpaceBetween>
  </ContentLayout></div>;
}

function ChecklistSection({ section }: { section: SetupSection }) {
  const navigate = useNavigate();
  const ready = section.checks.filter(check => check.status === "ready").length;
  return <Container header={<Header variant="h2" counter={`${ready}/${section.checks.length}`}>{section.title}</Header>}>
    <ul className="setup-checks">
      {section.checks.map(check => <li key={check.id} className="setup-check">
        <div className="setup-check-copy">
          <Box variant="h3">{check.label}</Box>
          <Box color="text-body-secondary">{check.detail}</Box>
        </div>
        <div className="setup-check-actions">
          <StatusIndicator type={status[check.status].type}>{status[check.status].text}</StatusIndicator>
          {check.action_href && check.action_label && (check.action_href.includes("#")
            ? <Button href={check.action_href}>{check.action_label}</Button>
            : <Button onClick={() => navigate(check.action_href!)}>{check.action_label}</Button>)}
        </div>
      </li>)}
    </ul>
  </Container>;
}
