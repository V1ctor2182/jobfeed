# Sanitized export boundaries

This repository starts with a new history and neutral commit author. It contains source code, operational rules, regression tests, ATS mappings and explicitly synthetic examples. The original repository history is not imported.

Excluded: actual applications, resumes and document metadata; original master/profile/contact/behavioral-answer files; credential and account files; local settings; runtime queues/databases, batch manifests, monitor output, screenshots, logs, backups and personal source documents. Personal authoring instructions were rewritten as reusable instructions. Hardcoded applicant identity was removed from agent prompts. BQ paths and Apple signing identity are configurable.

Synthetic examples are not confirmed facts and must never be submitted as an applicant's information. Operational rules preserve the original workflow; current authorization is required for every real batch and final submission.

Git ignore rules are prevention, not retroactive deletion or a security boundary. Review every future diff before publishing. Secrets must stay in approved local storage; do not place real stories in tracked documentation. GitHub account ownership remains visible to repository collaborators. This export does not scrub or delete the existing upstream repository.

The bundled BQ skill contains generic instructions, a behavioral-question reference map, source code and synthetic tests. Its original Git history, research sessions, cached answers, tmp files and candidate stories are excluded.
