# Submission confirmation evidence

A written success claim is not a submission receipt. Jobfeed marks a new application submitted only when the saved receipt has a readable confirmation artifact for the exact source posting. Missing, damaged, unrelated, or inaccessible evidence leaves **Submission unverified**. Reconcile the original page or email. Never submit again to obtain evidence.

Save evidence inside the current job's application folder. Keep the original tool output. Do not generate, redraw, edit, or substitute a screenshot. Do not write a simulated email from memory. Do not include passwords or verification codes. A result screenshot and an original confirmation email are alternatives.

## Website confirmation

Use the connected official browser tool's documented screenshot operation on the current job's confirmation tab. Save its returned image bytes unchanged as `confirmation.png`, `confirmation.jpg`, or `confirmation.jpeg`, matching the actual format. Use a browser page read to capture the site's explicit application acknowledgement. A third-party overlay, account-created message, uploaded résumé, disabled Submit button, or absence of errors is not acknowledgement.

Save `confirmation-page.json` with the exact text returned by the page tool and the observed URL. Record the real browser tab ID or tool result ID in `capture.source_id` and the tool name in `capture.tool`. Never invent a call ID. Use the same timezone-bearing capture timestamp in both JSON files. If the capture tool cannot provide a durable image, leave the result unverified.

`submission-receipt.json`:

```json
{
  "job_id": 123,
  "job_url": "https://careers.example.com/jobs/123",
  "confirmation": {
    "kind": "website_screenshot",
    "artifact": "confirmation.jpg",
    "observation": "confirmation-page.json",
    "captured_at": "2026-10-21T14:00:00-04:00",
    "source_url": "https://careers.example.com/jobs/123/confirmation",
    "text": "Your application has been received.",
    "capture": {"tool": "actual browser screenshot tool", "source_id": "actual tab or result ID"}
  }
}
```

The observation file contains `job_id`, `job_url`, `captured_at`, `source_url`, `text`, and `capture` with the same values. Replace every example value with the observed value. `job_url` must exactly match the saved Jobfeed source posting. Preserve existing receipt fields for synchronization and tab cleanup.

## Confirmation email

Use the connected mail tool to retrieve the original message, then save its original RFC 822 bytes as `confirmation.eml`. It must have From, To, Subject, Date, and Message-ID headers, a plain-text body that explicitly acknowledges the application, and the exact source posting URL in that body. Registration, verification, job alerts, and unrelated application messages do not qualify. If the message lacks the exact posting link, use the website screenshot path instead.

Use `kind: "confirmation_email"`, `artifact: "confirmation.eml"`, a capture timestamp, the actual message permalink in `source_url`, the mail tool name in `capture.tool`, and the original Message-ID in `capture.source_id`. Keep the top-level `job_id` and `job_url`. Do not reconstruct an email from an agent summary. HTML-only messages remain unverified; the dashboard never renders active email HTML or remote images.

### Email for an approved Submit

Many ATS acknowledgements name the company but not the posting link. Such an email counts only as `kind: "approved_submit_email"`, and only when all of these hold:

- The Applier runtime records the user's own Submit approval (Dashboard or Discord) and a Submit attempt it was spent on, in state `requested`, `unknown` or `submitted`. A job with no attempt, or one proved `not_submitted`, never qualifies.
- The email's `Date` is at or after that attempt and at most 24 hours later. Automated acknowledgements arrive within minutes; the day only absorbs a delayed mailer.
- The sender's domain is the posting's ATS mail domain (for example `ashbyhq.com` for an Ashby posting) or the employer's own domain.
- The subject or plain-text body names the job's company as Jobfeed stores it, and the body acknowledges the application without saying it failed.

The receipt is the same as above with the different `kind`. Every other rule above still applies.

The operator saves an email with `.venv/bin/python -m pipeline.cli record-confirmation-email JOB_ID --eml PATH --source-url PERMALINK --capture-tool TOOL`, where PATH holds the message's raw RFC 822 bytes exactly as the mail tool returned them, such as Gmail's RAW format. The command tries the link-bound kind first, keeps the files only when Jobfeed accepts one kind, and otherwise leaves the folder unchanged. When the Applier recorded the attempt as `unknown`, it settles it as `submitted`. Then run `finish-submission JOB_ID`.

## Recording and viewing

The original application agent saves the artifact and receipt. The controller calls `finish-submission JOB_ID`. Local validation runs again at the formal Jobfeed recording boundary before writing. The standalone Jobfeed `apply --confirmation-folder` command also requires that folder to be the existing job queue or closeout binding in the configured application runtime. This keeps every newly recorded result reachable from the dashboard. A later missing or corrupted artifact makes the dashboard show unverified evidence, while existing history remains intact and blocked from duplicate applications.

Open the job details in Apply health to view its screenshot or readable confirmation email, capture time, source URL, and source identifier. A user report alone still stops application actions but does not count as artifact-backed submission.

These checks establish that an inspectable, job-bound artifact exists and contains a recorded acknowledgement. Tool identifiers provide attribution, not remote-employer authentication. The screenshot and source remain visible for review.
