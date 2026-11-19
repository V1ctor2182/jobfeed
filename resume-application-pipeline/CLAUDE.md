# Resume authoring rules

Use `.claude/skills/tailor-resume/SKILL.md`. `master.md` and `profile.md` are verified local sources; `application-profile.md` contains confirmed form answers. None is distributed. All bundled CONTENT files are synthetic examples.

Select a verified category template by default; tailor only when requested. Never fabricate credentials, dates, metrics, work authorization, skills or experience. Keep a source pointer and matching autofill JSON for every selected PDF. Changes for one job stay in that job directory. The renderer uses minimum 10pt text; inspect the final PDF. Convert via tools/docx2pdf.sh on macOS with Word.

Application directories, local profiles, resumes, answers, receipts, credentials and runtime state must remain Git-ignored. Never commit real documents. Final submission requires explicit scoped authorization or the user's own auto submit mode. Autopilot mode does not change that.
