import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import type { ReactNode } from "react";
import type { BatchHealth } from "@/api/workspace";
import { formatLocalDateTime } from "@/lib/dates";
import { agentChatUrl } from "./run-model";

export function ApplicationsOverview({ batches, children }: { batches: BatchHealth[]; children: ReactNode }) {
  const latest = batches.filter(batch => batch.source_binding === "valid" && batch.controller)
    .sort((a, b) => (Date.parse(b.checked_at ?? "") || 0) - (Date.parse(a.checked_at ?? "") || 0) || b.id.localeCompare(a.id))[0];
  const controller = latest?.controller;
  const chat = agentChatUrl(controller?.thread_id);
  return <section id="application-overview" aria-label="Applications overview" tabIndex={-1}>
    <Container header={<Header variant="h2" actions={chat ? <Link href={chat}>Open controller chat</Link> : undefined}
      description={<>
        {controller ? <span>Latest recorded controller: <strong>{controller.display_name}</strong>.
          {controller.max_parallel != null ? ` Up to ${controller.max_parallel} concurrent ${controller.max_parallel === 1 ? "applier" : "appliers"}.` : " Capacity not recorded."}
          {" Worker saved status: "}<strong>{controller.worker_status === "stopped_by_user" ? "Stopped by user" : controller.worker_status === "launch_admitted" ? "Launch admitted" : "Not recorded"}</strong>.
          {` Last observation ${formatLocalDateTime(latest.checked_at ?? null)}.`}
        </span> : <span>No controller is recorded for these saved observations.</span>}
        <br /><Box variant="small">Queued jobs stay idle until you click Apply in Application queue. Earlier runs keep their latest saved observation. Saved states do not prove an applier is currently running.</Box>
      </>}>Applications</Header>}>
      {children}
    </Container>
  </section>;
}
