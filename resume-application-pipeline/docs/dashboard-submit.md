# Submit an application from the dashboard

1. Open the application in Runs.
2. Choose **Submit application**.
3. Review the job, warnings, exact answers, versions, and scopes.
4. Choose **Confirm and submit** to authorize this application.

Confirmation saves one request for the displayed answer versions and final submission for this job. The existing application owner performs the submission. If its worker is active, the request waits for the owner's current work and an available application slot. Other jobs continue. If the worker is stopped, this request can continue with the same owner without restarting the worker or the batch.

You can close the dashboard after confirming. Keep the Jobfeed backend running so its background reconciliation can advance saved requests when the original agent or a slot becomes available. A controller exiting normally does not require another confirmation. After a backend restart, reconciliation resumes from the durable request records.

Your confirmation has no time limit. It stays queued through connection outages and capacity waits until it can continue, unless you cancel or the reviewed application details change. The program rechecks authorization and those details before continuing. A changed or expired answer still requires review. If delivery or submission is uncertain, the program checks the original action without sending it again.

The program rechecks the exact reviewed answers, owner, resume and applicable workflow holds before sending. A change that invalidates the review blocks the request. Confirmation does not supply missing personal facts or authorize new prose. Browser security, CAPTCHA, account verification, and tool approval still apply when encountered.

The dashboard retains the submission request across refreshes in the same browser session. **Check submission status** follows that same request. A saved or queued request is not a submitted application. An uncertain delivery or submission attempt stays reserved for reconciliation and is never sent again. **Review current application** appears only when the program has verified that a blocked request was never sent. It opens a fresh review and still requires a new explicit confirmation.

Review saved fields, the resume and evidence in Runs. **Open application form** focuses the existing tab without taking browser control. Controllers and other assisting chats must not claim the applier's tab to inspect it; ask the original applier for fresh website verification. If the browser reports another chat owns the tab, report that specific blocker and preserve the tab. Do not keep reclaiming it, reload it or open a replacement.

A confirmed submission or existing submission evidence disables another submission request. The formal receipt and Jobfeed record remain separate evidence of completion. Agents must not invoke the dashboard confirmation adapter to manufacture a user approval.

For a Claude Applier, auto submit mode runs this same confirmation for you. Only the user turns it on, with `python -m pipeline.cli auto-submit on`. It confirms only a draft that is ready for review and would show no warning here, has no open question or blocker and no answer waiting for review, and whose Applier is neither stopped nor draining. Each Submit attempt gets at most one auto confirmation, and the job's Discord thread says when one was made. Agents must never turn the mode on.
