# Review and reuse application answers

Answers live in **My Profile** in the local Jobfeed dashboard, which is available before setup is complete. The old `/answer-bank` address opens the **Application answers** section.

- **Personal info** lists the voluntary self-identification questions (age, gender, Hispanic or Latino, race, veteran, disability, sexual orientation, transgender, pronouns).
- **Application answers** lists drafts an Applier wrote that are **Waiting for you**, the other common questions (work authorization, sponsorship, visa status, relocation, onsite work, preferred locations, start date, text message consent, background check, drug test, license, clearance, travel, salary, experience, graduation date, GPA, non-compete and more), and your other saved answers. **Add a question** saves a new question for every company.

Each answer is a question with one input and a **Save** button. Saving is your approval of exactly that answer. **Delete** revokes it so no Applier uses it. The page does not show versions; the store keeps every version as an audit record. Approval does not authorize submitting an application.

The common questions come from `pipeline/common_questions.py`. Each has a canonical wording, other wordings that forms use, and fixed options where the answer is one of a set. A saved answer to any wording of a common question answers its other wordings, so Appliers find it however a form phrases the question. When a form's options word the answer differently, the Applier selects the option with the same meaning.

Other equivalent questions are explicit variants stored with the answer. Similar words or a model's semantic judgment do not add a variant. A Claude Applier may still fill a saved answer whose question asks the same fact about the user in other words, but never for a question about the employer or role. When several approved answers at the same scope match, the newest wins. Company and job scopes prevent broader reuse. A job scope includes both company and job ID. Expiry is an ISO timestamp with an explicit timezone. A date without a time expires at the start of that date in UTC.

If another page changes the answers first, the server rejects the stale save and the row keeps your unsaved text. Reload the page before saving again.

## Answers an Applier decides under auto submit mode

A Claude Applier launched while your auto submit mode is on answers what it can itself, from My Profile, approved answers, the resumes and the posting, and fills short answers without asking you. It asks only for a fact that no material states and that a wrong guess would make matter, such as a license number or a legal attestation. Its `record_answer` tool saves each answer it decides as approved, with `applier_auto` as the approval source. A fact about you is scoped to every company, and a question about the employer to that company. Later Appliers reuse it like any approved answer.

Your own answers always win. An Applier's answer never replaces one you saved, gave in a reply, or deleted, and when both apply to a job, yours is used. Saving the row in My Profile makes it your answer. The job's Discord review summary and the Dashboard's Submit preview list every answer the Applier decided for that job, in full and as approved. With the mode off, the tool refuses and drafts wait for you as before.

## Agent draft intake

The canonical private store is `.pipeline/answer-bank.json`. It is ignored by Git. The old Markdown answer library and per-job `short-answer-review.json` files are historical materials, not approval authority.

BQ research saves new narrative answers as pending entries. A `ready` research response becomes `needs_review` until the exact current answer is approved. Applicable approved answers can be returned without another research call.

For a directly drafted answer outside BQ, use the pipeline interpreter and pass the exact question and text as JSON on stdin:

```sh
.venv/bin/python -m pipeline.answer_bank propose
```

```json
{
  "application_folder": "/absolute/pipeline/applications/current-job",
  "question": "Exact question from the form",
  "answer": "Complete proposed answer"
}
```

The command reads `application-task.json` in that folder and saves a pending answer scoped to its company and job. It cannot approve text. Repeating the same proposal does not duplicate the entry.

To import an old review file, run `python -m pipeline.answer_bank import-review` with `{"path":"/absolute/short-answer-review.json"}` on stdin. Imports preserve revision history and rejection metadata, but every current imported answer remains pending. Optional `scopes` maps stable entry IDs to proposed reuse scopes. Re-import does not overwrite later dashboard edits.

## Autofill enforcement

Official autofill paths check the current store before narrative writes and before accepting existing narrative values. They check exact answer text, approved question or explicit variant, scope, expiry, and visible length limits. Unknown text fields, textareas, and cover letters need approval. Narrowly identified structured fact fields remain eligible for the existing profile flow.

`verifyAutofillPage` rechecks visible narrative values from the page and records entry IDs and versions in application evidence. Gate failures remain in `validation.unresolved`, including when HTML required-field validity passes. Missing or malformed approval data and missing bound job context block narrative acceptance.

After upgrading a running application owner, reset its persistent JavaScript runtime and import the current Autofill module before any further field action. Preserve its browser tabs and draft. An old cached module does not gain the new checks. Acceptance rejects old page reports until a fresh verification records the approval inventory.

An existing answer that conflicts with a proposed replacement remains unresolved. Preserve the employer draft and review the conflict. Do not clear the field, refresh the form, or assume approval of a new draft also approves the old website text.

These checks protect the official pipeline and autofill entry points. They cannot prevent arbitrary local code or a person using the browser directly. Saving in the local dashboard (the `save` action, or the older `approve`) is the supported approval boundary. The backend checks the same loopback origin, then records a generated approval event ID, timestamp, exact version, and local-user source. The ordinary CLI can neither save nor approve answers. Its `dashboard-mutate` adapter entry is reserved for the dashboard backend and is not evidence of a chat approval. Agents must never invoke it to approve their own prose.

The Rilla batch remains stopped before submission. Editing or approving an answer neither resumes that batch nor changes `submit_authorized`.
