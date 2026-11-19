---
name: tailor-resume
description: Select or tailor a truthful resume for a job description, build its document and matching autofill sidecar, and hand it to the application pipeline.
---

# Tailor resume

Read CLAUDE.md. Use only verified local master.md, profile.md, and application-profile.md facts. The bundled bases are synthetic examples, not applicant facts. Never invent experience, skills, eligibility, dates, metrics or authorization.

1. Read the job description and record company, title, URL, hard requirements and `Jobfeed Job ID: <id>` in `applications/<Company>-<Role>-<date>/jd.md`. Do not duplicate a job already owned by another session.
2. Check requirements against confirmed facts. Ask for missing essential facts rather than infer them. Choose the closest class from bases/README.md. Default to a verified class PDF; customize CONTENT only if requested. Use the applicant's confirmed graduation date, never choose a different date to pass a gate.
3. In tailor mode copy that class's content.py into the job directory. Map job keywords to evidence, select relevant achievements, and record factual gaps. Preserve facts; revise emphasis and wording only. Use readable bullets and a one-page layout with at least 10pt type. Do not change master files for a single application.
4. Build with `python tools/build_resume_docx.py templates/resume.docx <content.py> <output.docx>`. Convert using `tools/docx2pdf.sh <output.docx>` (macOS Microsoft Word). Check with `python tools/check_resume_pdf.py <output.pdf> --content <content.py> --name '<confirmed display name>'`, render with pdftoppm, and visually inspect. Fix layout before delivery.
5. Save resume-source.json containing `content` (relative source path), `variant` (matching CONTENT), and `resume` (relative PDF path). Generate `<PDF stem>.autofill.json` from that exact CONTENT using tools/content_autofill.py. Never reconstruct facts from a PDF. Current date variants are legacy Dec2026/May2027 examples; update variant handling consistently for other dates.
6. Save tailored.md with `Verdict: apply`, `apply with caveat`, or `do not apply`, requirements, template selection, keyword coverage and changes. Deliver the PDF and source paths with validation results.
7. Only for a pipeline-dispatched job, after all material checks pass, create `.complete` in that job folder. The application agent reads application-task.json and its submit authorization. Resume readiness does not authorize submission.

For repeat applications, preserve truthful identity and disclose previous applications when asked. Never invent alternate contact data or bypass duplicate-account restrictions.
