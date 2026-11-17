import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Textarea from "@cloudscape-design/components/textarea";
import { useState } from "react";
import { useNavigate } from "react-router";
import type { JobProfile } from "@/api/onboarding-resume";
import { type ProfilePreferences, useSaveProfilePreferences } from "@/api/my-profile-preferences";

const listFields = [
  ["desired_titles", "Desired job titles"], ["seniority_levels", "Seniority or level"],
  ["target_countries", "Target countries"], ["target_locations", "Target locations"],
  ["preferred_locations", "Preferred locations"],
  ["industries", "Industries"], ["company_sizes", "Company sizes"],
  ["excluded_titles", "Excluded titles"], ["excluded_companies", "Excluded companies"],
  ["excluded_locations", "Excluded locations"], ["excluded_keywords", "Excluded keywords"],
] as const;

export function JobPreferencesEditor({ state, confirmed }: { state: ProfilePreferences; confirmed: boolean }) {
  const [draft, setDraft] = useState(state.profile);
  const [saved, setSaved] = useState(state.profile);
  const [received, setReceived] = useState(state.profile);
  const save = useSaveProfilePreferences();
  const navigate = useNavigate();
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  if (received !== state.profile && !dirty) {
    setReceived(state.profile);
    setDraft(state.profile);
    setSaved(state.profile);
  }

  function update<K extends keyof JobProfile>(key: K, value: JobProfile[K]) {
    save.reset();
    setDraft(current => ({ ...current, [key]: value }));
  }

  return <section id="job-preferences" aria-label="Job preferences">
    <Container header={<Header variant="h2" description="Edit your existing preferences directly. AI analysis is not required.">Job preferences</Header>}>
      <SpaceBetween size="m">
        <ColumnLayout columns={2} minColumnWidth={280}>
          {listFields.map(([key, label]) => <FormField key={key} label={label} description={key === "target_countries" ? "Restrict results to these countries. For all US locations, enter United States and leave Target locations empty." : key === "target_locations" ? "Optional restriction to specific cities or regions. When filled, these replace the country-wide location filter." : key === "preferred_locations" ? "Optional preferences, such as California and New York. Saved for reference; they do not restrict results or change ranking." : "One value per line"}>
            <Textarea value={(draft[key] ?? []).join("\n")} disabled={save.isPending}
              onChange={({ detail }) => update(key, detail.value.split("\n"))} />
          </FormField>)}
        </ColumnLayout>
        <Box>Work authorization, sponsorship, and availability are edited in <Link href="/profile?section=details" onFollow={(event) => { event.preventDefault(); navigate("/profile?section=details"); }}>Profile</Link>.</Box>
        <FormField label="Work modes">
          <SpaceBetween direction="horizontal" size="m">
            {(["remote", "hybrid", "on-site"] as const).map(mode => <Checkbox key={mode} checked={draft.work_modes.includes(mode)} disabled={save.isPending}
              onChange={({ detail }) => update("work_modes", detail.checked ? [...draft.work_modes, mode] : draft.work_modes.filter(item => item !== mode))}>{mode}</Checkbox>)}
          </SpaceBetween>
        </FormField>
        <FormField label="Maximum posting age in hours" description="Between 1 and 8,760 hours. Indeed uses its source-specific search window.">
          <Input type="number" value={String(draft.maximum_posting_age_hours)} disabled={save.isPending} nativeInputAttributes={{ min: 1, max: 8760 }}
            onChange={({ detail }) => { save.reset(); setDraft(current => ({ ...current, maximum_posting_age_hours: Number(detail.value), maximum_posting_age_days: null })); }} />
        </FormField>
        <Box color="text-body-secondary">Saving applies location, company, and posting-age filters and the uploaded scoring résumé. Titles and other preferences are saved for search setup.</Box>
        {!confirmed && <Alert type="info">Save job preferences to confirm this draft before reviewing searches.</Alert>}
        {!state.scoring_resume_active && <Alert type="info">The uploaded résumé is not active for scoring yet. Upload a résumé if needed, then save preferences.</Alert>}
        {state.searches_need_review && <Alert type="warning" header="Discovery searches need review" action={<Button onClick={() => navigate("/setup/searches")}>Review searches</Button>}>
          Your existing searches have been kept. Review and save the search selections to match your preferences, then apply them in the setup review.
        </Alert>}
        {save.error && <Alert type="error" header="Job preferences could not be saved">{save.error.message} Your edits are still here.</Alert>}
        {save.isSuccess && !dirty && <Alert type="success">Job preferences saved. Location, company, and posting-age filters are applied. Review searches to use updated discovery preferences.</Alert>}
        <SpaceBetween size="s" direction="horizontal">
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(draft, { onSuccess: result => { setDraft(result.profile); setSaved(result.profile); } })}>Save job preferences</Button>
          <Button disabled={dirty || save.isPending || !state.scoring_resume_active || !confirmed} onClick={() => navigate("/setup/searches")}>Continue to review searches</Button>
          {dirty && <Box color="text-body-secondary">Unsaved changes</Box>}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  </section>;
}
