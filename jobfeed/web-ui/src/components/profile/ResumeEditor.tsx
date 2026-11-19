import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import Container from "@cloudscape-design/components/container";
import FileUpload from "@cloudscape-design/components/file-upload";
import Link from "@cloudscape-design/components/link";
import Header from "@cloudscape-design/components/header";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { myProfileKey, type MyProfile, type ResumeEntry, useSetResumeActive } from "@/api/my-profile";
import { useUploadResume } from "@/api/onboarding-resume";
import { checklistKey } from "@/api/checklist";
import { ResumeLibraryForm } from "./ResumeLibraryForm";

function googleDocPdfExportUrl(source: string): string {
  const url = new URL(source);
  url.pathname = url.pathname.replace(/\/edit$/, "/export");
  url.search = new URLSearchParams({ format: "pdf" }).toString();
  url.hash = "";
  return url.toString();
}

export function ResumeEditor({ resumes }: { resumes: MyProfile["resumes"] }) {
  const [files, setFiles] = useState<File[]>([]);
  const upload = useUploadResume();
  const availability = useSetResumeActive();
  const client = useQueryClient();
  const error = upload.error ?? availability.error;
  const [editing, setEditing] = useState<{ kind: "master" | "variant"; entry?: ResumeEntry } | null>(null);
  const master = resumes.find(resume => resume.kind === "master");
  const variants = resumes.filter(resume => resume.kind === "variant");
  const legacy = resumes.filter(resume => !resume.kind || resume.kind === "legacy");
  const details = (resume: ResumeEntry) => <SpaceBetween size="xs">
    <Box fontWeight="bold">{resume.label || resume.filename}</Box>
    {resume.role && <Box>{resume.role}</Box>}
    <Box color="text-body-secondary">{resume.status}</Box>
    <SpaceBetween direction="horizontal" size="m">
      {resume.google_doc_url && <Button href={googleDocPdfExportUrl(resume.google_doc_url)} external
        ariaLabel={`Sync ${resume.label || resume.filename}`}>Sync</Button>}
      {resume.google_doc_url && <Link external href={resume.google_doc_url}>Open Google Doc</Link>}
      {resume.download_url && <Link href={resume.download_url}>{resume.format === "pdf" ? "Download saved PDF" : "Download saved source"}</Link>}
    </SpaceBetween>
    {resume.pdf_export && <Box variant="small" color="text-body-secondary">
      {resume.page_count} page. {resume.pdf_export.provenance === "observed_google_docs_export" ? "Google Docs export observed by agent." : "Export association confirmed by uploader."}
      {resume.pdf_export.exported_at ? ` Exported ${new Date(resume.pdf_export.exported_at).toLocaleString()}.` : " Export time not recorded."}
    </Box>}
    {resume.kind === "variant" && <Checkbox checked={resume.active} disabled={availability.isPending || (!resume.application_supported && !resume.active)}
      onChange={({ detail }) => availability.mutate({ id: resume.id, active: detail.checked })}>Available for future applications</Checkbox>}
    <Button onClick={() => setEditing({ kind: resume.kind === "master" ? "master" : "variant", entry: resume })}>Edit or replace {resume.kind === "master" ? "master" : resume.label || resume.filename}</Button>
  </SpaceBetween>;

  return <section id="resume" aria-label="Résumé">
    <SpaceBetween size="l">
    <Box color="text-body-secondary">Sync downloads the latest Google Docs PDF. Use Edit or replace to update the saved agent copy.</Box>
    <Container header={<Header variant="h2" description="All your experience, projects, and bullet points in one source.">Master résumé</Header>}>
      {master ? details(master) : <SpaceBetween size="m"><Box>No master saved yet. Add the complete source you use to prepare role-specific résumés.</Box><Button onClick={() => setEditing({ kind: "master" })}>Add master</Button></SpaceBetween>}
    </Container>
    <Container header={<Header variant="h2" description="One-page PDFs exported from their linked Google Docs. Agents use the downloaded PDFs." actions={<Button onClick={() => setEditing({ kind: "variant" })}>Add variant</Button>}>Application variants</Header>}>
      <SpaceBetween size="l">
        {variants.length ? variants.map(resume => <div key={resume.id}>{details(resume)}</div>) : <Box>No variants saved yet. Add SWE, AI Infrastructure, AI Engineer, or another target role.</Box>}
        <Box color="text-body-secondary">Export records describe the saved PDF. Later edits in Google Docs require a new PDF export.</Box>
      </SpaceBetween>
    </Container>
    {editing && <Modal visible onDismiss={() => setEditing(null)} closeAriaLabel="Close résumé editor"
      header={`${editing.entry ? "Update" : "Add"} ${editing.kind === "master" ? "master résumé" : "application variant"}`}>
      <ResumeLibraryForm key={`${editing.kind}-${editing.entry?.id ?? "new"}`} {...editing} onClose={() => setEditing(null)} />
    </Modal>}
    <Container header={<Header variant="h2" description="Existing uploads retain their current availability.">Other uploaded résumés</Header>}>
      <SpaceBetween size="m">
        <Box>Active PDFs are available for future applications when you authorize them. Existing applications keep their previously selected files.</Box>
        {legacy.length === 0 ? <Box color="text-body-secondary">No other résumés uploaded.</Box> : legacy.map(resume =>
          <div key={resume.id}>
            <Checkbox checked={resume.active} disabled={availability.isPending}
              onChange={({ detail }) => availability.mutate({ id: resume.id, active: detail.checked })}>
              {resume.filename} — {resume.active ? "Active" : "Inactive"}
            </Checkbox>
            {resume.status && <Box variant="small" color="text-body-secondary">{!resume.active && resume.status === "Available for applications" ? "Unavailable for future applications" : resume.status}</Box>}

          </div>)}
        <FileUpload value={files} accept=".pdf,.docx,.md,.txt" showFileSize
          constraintText="PDF, DOCX, Markdown, or plain text. Maximum 10 MB. Uploading the same filename replaces its saved version; other filenames stay in this list. Applications require a text-based PDF."
          i18nStrings={{ uploadButtonText: () => "Choose résumé file", dropzoneText: () => "Drop a résumé here", removeFileAriaLabel: (_index, name) => `Remove ${name}`, errorIconAriaLabel: "Error" }}
          onChange={({ detail }) => { upload.reset(); setFiles(detail.value.slice(-1)); }} />
        <Button loading={upload.isPending} disabled={files.length !== 1} onClick={() => upload.mutate(files[0]!, {
          onSuccess: () => {
            setFiles([]);
            void client.invalidateQueries({ queryKey: myProfileKey });
            void client.invalidateQueries({ queryKey: ["my-profile-preferences"] });
            void client.invalidateQueries({ queryKey: checklistKey });
          },
        })}>Upload résumé</Button>
        {upload.isSuccess && <Alert type="success">Résumé saved in your list. Save job preferences to use the uploaded text for scoring. Your saved preferences were kept.</Alert>}
        <Box color="text-body-secondary">Uploading does not send your résumé to an AI provider. Save job preferences to activate the uploaded text for scoring.</Box>
        <Link href="/setup/resume">Optional résumé analysis</Link>
        {error && <Alert type="error" header="Résumé change could not be saved">{error.message}</Alert>}
      </SpaceBetween>
    </Container>
    </SpaceBetween>
  </section>;
}
