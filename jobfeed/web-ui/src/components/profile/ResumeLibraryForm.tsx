import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import FileUpload from "@cloudscape-design/components/file-upload";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { type ResumeEntry, useUpdateResumeDetails, useUploadLibraryResume } from "@/api/my-profile";

export function ResumeLibraryForm({ kind, entry, onClose }: {
  kind: "master" | "variant"; entry?: ResumeEntry; onClose: () => void;
}) {
  const [label, setLabel] = useState(entry?.label ?? "");
  const [role, setRole] = useState(entry?.role ?? "");
  const [url, setUrl] = useState(entry?.google_doc_url ?? "");
  const [files, setFiles] = useState<File[]>([]);
  const [declared, setDeclared] = useState(false);
  const upload = useUploadLibraryResume();
  const update = useUpdateResumeDetails();
  const client = useQueryClient();
  const pending = upload.isPending || update.isPending;
  const metadata = { kind, label, role, google_doc_url: url };
  const saved = () => {
    void client.invalidateQueries({ queryKey: ["onboarding", "resume"] });
    void client.invalidateQueries({ queryKey: ["my-profile-preferences"] });
    onClose();
  };
  return <SpaceBetween size="m">
    <FormField label="Resume name"><Input value={label} onChange={({ detail }) => setLabel(detail.value)} /></FormField>
    {kind === "variant" && <FormField label="Target role" description="For example, Software Engineer, AI Infrastructure, or AI Engineer.">
      <Input value={role} onChange={({ detail }) => setRole(detail.value)} />
    </FormField>}
    <FormField label="Google Docs URL" description={kind === "master" ? "Your complete source document." : "The document used to export this PDF. Changing this URL requires a new PDF export."}>
      <Input value={url} type="url" onChange={({ detail }) => setUrl(detail.value)} />
    </FormField>
    <FileUpload value={files} accept={kind === "master" ? ".pdf,.docx,.md,.txt" : ".pdf"} showFileSize
      constraintText={kind === "master" ? "All projects and bullet points. PDF, DOCX, Markdown, or text, up to 10 MB." : "Download a one-page PDF from the linked Google Doc, then upload it here. Up to 10 MB."}
      i18nStrings={{ uploadButtonText: () => kind === "master" ? "Choose master file" : "Choose exported PDF", dropzoneText: () => "Drop a file here", removeFileAriaLabel: (_index, name) => `Remove ${name}`, errorIconAriaLabel: "Error" }}
      onChange={({ detail }) => { setFiles(detail.value.slice(-1)); setDeclared(false); }} />
    {kind === "variant" && files.length > 0 && <Checkbox checked={declared} onChange={({ detail }) => setDeclared(detail.checked)}>
      This PDF was exported from the Google Doc linked above.
    </Checkbox>}
    <SpaceBetween size="xs" direction="horizontal">
      <Button variant="primary" loading={pending} disabled={!label.trim() || (kind === "variant" && !role.trim()) || (!entry && !files.length) || (kind === "variant" && files.length > 0 && (!url.trim() || !declared))}
        onClick={() => files[0]
          ? upload.mutate({ file: files[0], identifier: entry?.id, metadata: { ...metadata, provenance: kind === "variant" && declared ? "user_declared" : null } }, { onSuccess: saved })
          : entry && update.mutate({ id: entry.id, metadata }, { onSuccess: saved })}>
        {files.length ? "Save file and details" : "Save details"}
      </Button>
      <Button disabled={pending} onClick={onClose}>Cancel</Button>
    </SpaceBetween>
    {(upload.error || update.error) && <Alert type="error">{(upload.error || update.error)?.message}</Alert>}
  </SpaceBetween>;
}
