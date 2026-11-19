import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import { useNavigate } from "react-router";
import { useProfilePreferences } from "@/api/my-profile-preferences";
import type { ConfigurationResponse } from "@/api/configuration";

export function PreferenceSummary({ configuration }: { configuration: ConfigurationResponse }) {
  const navigate = useNavigate();
  const preferences = useProfilePreferences();
  const filters = configuration.hard_filters!;
  const hours = filters.posted_within_hours
    ?? (filters.posted_within_days == null ? null : filters.posted_within_days * 24);
  const values = [
    ["Allowed locations", filters.location_allowlist?.join(", ") || "All locations"],
    ["Preferred locations (saved preference)", preferences.isPending ? "Loading…" : preferences.isError ? "Unavailable" : preferences.data?.profile?.preferred_locations?.join(", ") || "None"],
    ["Excluded locations", filters.location_blocklist?.join(", ") || "None"],
    ["Excluded companies", filters.company_blocklist?.join(", ") || "None"],
    ["Excluded titles", filters.title_blocklist?.join(", ") || "None"],
    ["Maximum posting age", hours == null ? "No hourly limit" : `${hours} hours`],
  ];
  return <Container header={<Header variant="h2"
    description="Current filters are managed in My Profile. Indeed uses its source-specific search window."
    actions={<Button formAction="none" onClick={() => navigate("/profile?section=preferences")}>Edit job preferences</Button>}>Job preferences</Header>}>
    <ColumnLayout columns={2} variant="text-grid">
      {values.map(([label, value]) => <div key={label}><Box variant="strong">{label}</Box><Box>{value}</Box></div>)}
    </ColumnLayout>
  </Container>;
}
