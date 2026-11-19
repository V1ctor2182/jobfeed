import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { type MyProfile, type PersonalDetails, useSavePersonalDetails } from "@/api/my-profile";

const fields: { key: keyof PersonalDetails; label: string; description?: string; type?: "email" | "url" | "text" }[] = [
  { key: "legal_name", label: "Legal name" },
  { key: "preferred_name", label: "Preferred name" },
  { key: "primary_email", label: "Email", type: "email" },
  { key: "phone", label: "Phone" },
  { key: "address", label: "Address", description: "Use the location or full address you want included in applications." },
  { key: "linkedin_url", label: "LinkedIn URL", type: "url", description: "Include https:// at the beginning." },
  { key: "portfolio_url", label: "Portfolio URL", type: "url", description: "Include https:// at the beginning." },
  { key: "github_url", label: "GitHub URL", type: "url", description: "Include https:// at the beginning." },
  { key: "work_authorization", label: "Work authorization", description: "Enter your actual status. Leave blank if you have not decided what to provide." },
  { key: "sponsorship_requirement", label: "Sponsorship requirement" },
  { key: "relocation", label: "Relocation preference" },
  { key: "availability", label: "Availability or start date" },
  { key: "education", label: "Education", description: "School, degree, and graduation information on one line." },
];

export function PersonalDetailsEditor({ initial }: { initial: MyProfile }) {
  const save = useSavePersonalDetails();
  const [draft, setDraft] = useState(initial.personal_details);
  const [saved, setSaved] = useState(initial.personal_details);
  const [revision, setRevision] = useState(initial.revision);
  const [reviewed, setReviewed] = useState(initial.reviewed);
  const [errors, setErrors] = useState<Partial<Record<keyof PersonalDetails, string>>>({});
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const conflict = save.error instanceof ApiError && save.error.status === 409;

  function submit() {
    const invalid = validate(draft);
    setErrors(invalid);
    if (Object.keys(invalid).length > 0) return;
    save.mutate({ personal_details: draft, revision }, {
      onSuccess: (state) => {
        setDraft(state.personal_details);
        setSaved(state.personal_details);
        setRevision(state.revision);
        setReviewed(state.reviewed);
      },
    });
  }

  return <section id="personal-details" aria-label="Personal details">
    <Container header={<Header variant="h2" description="Review your saved information. Blank answers remain unknown.">Personal details</Header>}>
      <SpaceBetween size="m">
        {!reviewed && <Alert type="info">Your existing profile has been loaded. Review it and save to mark these details as reviewed.</Alert>}
        <ColumnLayout columns={2} minColumnWidth={280}>
          {fields.map(field => <FormField key={field.key} label={field.label} description={field.description} errorText={errors[field.key]}>
            <Input value={draft[field.key]} type={field.type ?? "text"} disabled={save.isPending}
              onChange={({ detail }) => { save.reset(); setErrors(current => ({ ...current, [field.key]: undefined })); setDraft({ ...draft, [field.key]: detail.value }); }} />
          </FormField>)}
        </ColumnLayout>
        <Box color="text-body-secondary">Additional saved notes and account credentials are preserved. Credentials are not displayed here.</Box>
        {save.error && <Alert type="error" header={conflict ? "The saved profile changed" : "Personal details could not be saved"}>
          {conflict ? "Your edits are still here. Copy any changes you want to keep, then reload this page to review the latest saved details before saving again." : save.error.message}
        </Alert>}
        {save.isSuccess && !dirty && <Alert type="success">Personal details saved and reviewed.</Alert>}
        <SpaceBetween size="s" direction="horizontal">
          <Button variant="primary" loading={save.isPending} disabled={!dirty && reviewed} onClick={submit}>Save personal details</Button>
          {dirty && <Box color="text-body-secondary">Unsaved changes</Box>}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  </section>;
}

function validate(details: PersonalDetails) {
  const errors: Partial<Record<keyof PersonalDetails, string>> = {};
  for (const { key } of fields) {
    if (details[key].length > 2000) errors[key] = "Use 2,000 characters or fewer.";
    else if (/[\n\r\0]/.test(details[key])) errors[key] = "Use one line for this answer.";
  }
  if (details.primary_email.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(details.primary_email.trim())) {
    errors.primary_email = "Enter an email address such as name@example.com.";
  }
  for (const key of ["linkedin_url", "portfolio_url", "github_url"] as const) {
    const value = details[key].trim();
    if (!value) continue;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password || /\s/.test(value)) throw new Error();
    } catch {
      errors[key] = "Enter a complete http:// or https:// website URL.";
    }
  }
  return errors;
}
