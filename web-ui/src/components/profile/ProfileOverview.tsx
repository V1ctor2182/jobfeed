import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useNavigate } from "react-router";
import type { ResumeEntry } from "@/api/my-profile";
import { profileStrength, type StrengthInput } from "./profile-strength";
import { type ProfileSection, sectionHref } from "./sections";

export function ProfileOverview({ input, activeResume }: { input: StrengthInput; activeResume?: ResumeEntry }) {
  const navigate = useNavigate();
  const { percent, items } = profileStrength(input);
  const open = (section: ProfileSection) => (event: CustomEvent) => { event.preventDefault(); navigate(sectionHref(section)); };
  return <SpaceBetween size="l">
    <Container header={<Header variant="h2" description="How much of what applications ask is already in your profile.">Profile strength</Header>}>
      <SpaceBetween size="m">
        <ProgressBar value={percent} label="Profile strength" additionalInfo={percent === 100 ? "Your profile is complete." : undefined} />
        <ul className="profile-checklist" aria-label="Profile checklist">
          {items.map((item) => <li key={item.label}>
            {item.complete
              ? <StatusIndicator type="success">{item.label}</StatusIndicator>
              : <SpaceBetween direction="horizontal" size="xs"><Link href={sectionHref(item.section)} onFollow={open(item.section)}>{item.label}</Link><Box color="text-status-info" fontWeight="bold">+{item.weight}%</Box></SpaceBetween>}
          </li>)}
        </ul>
      </SpaceBetween>
    </Container>
    <Container header={<Header variant="h2" actions={<Link href={sectionHref("resume")} onFollow={open("resume")}>Manage résumés</Link>}>Résumé</Header>}>
      {activeResume
        ? <SpaceBetween size="xs">
          <SpaceBetween direction="horizontal" size="xs"><Box fontWeight="bold">{activeResume.label || activeResume.filename}</Box><Badge color="green">Active</Badge></SpaceBetween>
          {activeResume.label && <Box color="text-body-secondary">{activeResume.filename}</Box>}
          {activeResume.pdf_export?.exported_at && <Box color="text-body-secondary">Exported {new Date(activeResume.pdf_export.exported_at).toLocaleString()}</Box>}
          {activeResume.download_url && <Link href={activeResume.download_url} external>Open résumé</Link>}
        </SpaceBetween>
        : <Box>No résumé is available for applications yet. <Link href={sectionHref("resume")} onFollow={open("resume")}>Upload a résumé</Link></Box>}
    </Container>
  </SpaceBetween>;
}
