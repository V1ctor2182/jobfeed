# Approved short-answer bank

Store this private file at `docs/application-bq-answer-library.md`. Keep personal answers and approval records out of Git. Keep pending drafts in each job's `short-answer-review.json`.

Only explicitly approved answer versions belong here. A confirmed fact, a generated `ready` answer, or a prior application is not prose approval. Reuse exact approved wording only when the question's meaning, relevant facts, scope, and length limit still fit. Otherwise request review again.

An approved entry records these fields:

- Answer ID and version.
- Status, either approved, superseded, or withdrawn.
- Original question and its intent.
- Exact approved answer text.
- Fact sources and any facts that can become stale.
- Reuse scope, including company or role restrictions.
- Real approval thread ID, message ID, user wording, and receipt time.
- IDs and versions superseded by this entry, if any.

On reuse, record the answer ID, version, target question, and match rationale in the application's answer record. Do not silently rewrite or shorten an approved answer.
