# Existing resume selection

New application runs use existing PDF files. Each Claude Applier chooses the resume that fits its job. Python freezes the available files, verifies the selection, and supplies a private upload copy. The pipeline does not open Claude resume sessions, generate DOCX files, invoke Word, or wait for `.complete` markers on this path.

## Local catalog

The private `resume-catalog.json` lives in the pipeline repository root and is ignored by Git. Its entries explicitly list available files. The pipeline does not discover and enable every PDF in a directory.

```json
{
  "schema_version": 1,
  "resumes": [
    {
      "id": "general",
      "label": "General software engineer resume",
      "available": true,
      "pdf": "approved-resumes/general.pdf",
      "description": "Existing resume for backend and product engineering roles."
    }
  ]
}
```

Paths are relative to the catalog file unless absolute. Optional `text` and `autofill` paths identify existing matching UTF-8 text and structured Autofill facts. These companions must describe the same PDF. An unavailable entry cannot be selected.

The local workspace catalog initially contains the supplied original PDF. The three annotated role drafts are excluded because their README identifies unresolved markers and a notes page. Catalog availability does not certify factual completeness or authorize a submission.

## Choice and recovery

A batch manifest includes its frozen catalog. Each job's prepared files and selected PDF remain isolated from other Appliers.

The Applier calls `list_resumes`, compares the job with the available resumes, and calls `select_resume` once with one ID and a reason. The tool checks the owner, job, scope, and file identity before publishing `resume-source.json`, and returns the exact file to upload. A validation failure stops the action.

The owner is the Applier's launch ID. A choice for a different owner or an unrecorded file is refused.

The receipt uses `kind: selected_existing`, binds the job and original owner, and identifies the chosen catalog entry and copied assets. Recovery reuses the recorded choice. A contradictory replay or changed selected file fails validation. The source PDF is never rewritten.

## Facts and browser work

An existing matching Autofill companion supplies resume facts. Without one, the program supplies only neutral `schemaVersion: 1` metadata for upload and confirmed profile fields. Empty metadata makes no education, experience, or date claims. Required missing facts remain real blockers; the pipeline does not fabricate CONTENT, graduation variants, or resume text.

The Applier chooses the resume before upload and uploads the selected PDF. Selection does not expand final-submit permission.

## Submission and Jobfeed

Website success, formal Jobfeed recording, and tab cleanup remain separate steps. The existing website receipt and read-back checks still apply.

Jobfeed receives the selected catalog ID and PDF identity as provenance. A PDF is never passed to a text-only `--tailored` argument, and the absence of `tailored.md` does not block the selected-existing path. Jobfeed's configured master-resume snapshot is a separate record and must not be described as the uploaded PDF's identity.

## Historical records

Existing Tailoring and Dispatching records remain available for reconciliation. Normal new runs do not resume that production loop. Historical authoring tools and tests remain in the repository; they are not application dependencies.

The resume change does not approve jobs, arm a batch, or start a worker. Validation uses temporary files, synthetic queues, and captured service calls.

## Master and role-specific Google Docs exports

My Profile separates one complete master from application variants. The master
contains all projects and bullet points. It is source material for scoring and
resume preparation, and it never enters the selectable application catalog.

Each variant records its target role, Google Docs URL, and a downloaded one-page
text PDF. The library stores the PDF export association with the saved file name,
source URL, page count, optional export time, and provenance. Browser uploads use
`user_declared` provenance after the uploader confirms the source. A local agent
can register `observed_google_docs_export` after observing the actual download.
An unknown export time stays unknown. These records do not claim that later
changes in Google Docs have been downloaded.

Use **Add master** to store the complete source. Use **Add variant** for SWE,
AI Infrastructure, AI Engineer, or another role. Download the PDF from the linked
Google Doc before uploading it. Editing the source URL clears the previous
export association and disables that variant until a matching PDF is registered.
Replacing a variant does not replace the scoring master. Existing unclassified
uploads retain their current availability.

The application catalog includes role and source metadata for eligible variants.
The frozen catalog preserves that metadata with the PDF selected by an agent.
Master uploads and variant edits do not start applications or replace previously
selected files. Changing the current catalog still requires the existing explicit
admission-scope reconciliation. Do not reset frozen catalogs to bypass that guard.
