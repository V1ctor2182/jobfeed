# Autopilot

Autopilot mode is your standing authorization for the whole path from scan to apply. While it is on, a daily timer scans every enabled source and evaluates new jobs, the worker starts the same scan early when no eligible job is left, and one continuous Claude batch applies to every Results job whose Stage B verdict is **apply** or **consider**, except jobs on the [recruiter sheet](recruiter-sheet.md). There is no daily cap.

Autopilot does not change Submit. Each Submit still needs your own confirmation or auto submit mode, with the same checks. A job with a CAPTCHA, an open question or blocker, an answer waiting for review, or a Dashboard warning still waits for you.

## Turn it on and off

Run these from `resume-application-pipeline`:

```sh
.venv/bin/python -m pipeline.cli autopilot on
.venv/bin/python -m pipeline.cli autopilot off
.venv/bin/python -m pipeline.cli autopilot status
```

Only you turn it on. An agent runs `on` only when you ask it to. Anyone may turn it off, and `off --by NAME` records who did. `status` shows who changed it last and when.

When you turn it off, nothing new is admitted or started. Appliers already working finish as usual, and the worker keeps supervising them. The worker exits once no started job needs it. A job admitted but not yet started stays in the batch and starts when you turn autopilot on again.

## What runs

`jobfeed-autopilot-cycle.timer` runs `jobfeed-autopilot-cycle.service` once a day, at a random time between 08:00 and 10:00, and catches up after the computer was off. The service runs `jobfeed/scripts/jobfeed_scheduled_cycle.py`. The script skips the day when another scan or evaluation is running. It never retries a paid evaluation. It checks the Jobright extension only when the Jobright source is enabled in `jobfeed/config.toml`. The service is skipped while autopilot is off.

The worker also starts `jobfeed-autopilot-cycle.service` when an Applier slot is free and Jobfeed has no eligible job left to admit, unless a scan started in the last three hours. It checks again every 15 minutes while the queue stays dry. The jobs that scan rates **apply** or **consider** are admitted as soon as it finishes. Only the worker under `jobfeed-autopilot.service` does this, because its unit sets `JOBFEED_AUTOPILOT_SCAN_UNIT`. Each scan's evaluation spends model credits, about $1 to $2 for a day's new jobs.

When an Applier clicked Submit but cannot see whether it went through, the worker checks Gmail every 10 minutes for the employer's confirmation email, through the same Gmail connector the account program uses. A message counts only when Jobfeed's evidence check accepts it as this job's confirmation (`pipeline/confirmation_email.py`). Then the question is answered "submitted" from source `email`, the email is saved as the job's evidence, and the Applier records the outcome and closes its tabs. The question reaches you in Discord only if no such email arrived within 12 hours. The worker does this only under `jobfeed-autopilot.service`, whose unit sets `JOBFEED_CONFIRMATION_MAIL`.

`jobfeed-autopilot.service` runs `python -m pipeline.autopilot serve`. While autopilot is on and no other application worker runs, it starts the worker for the continuous batch `.pipeline/batches/autopilot.json`, with four Claude Appliers and résumés chosen from `resume-catalog.json`. The worker log is `monitor-output/autopilot/worker.log`.

The batch takes eligible Results jobs in triage score order, User priority jobs first. It skips jobs that are already queued with another state, applied, closed, stale, ignored, held for a CAPTCHA, at an excluded company, or on the recruiter sheet. It admits a job only when an Applier slot is free.

Only one application worker runs at a time. A Dashboard **Start with Claude** stops the autopilot worker with one SIGINT and starts its own. Autopilot starts again after that worker exits. While it waits, it takes over a Dashboard worker whose jobs all have started Appliers, the same rule a Dashboard start uses. The Dashboard keeps showing that start as preparing, because the autopilot worker supervises its Appliers.

If the worker exits with an error three times in a row, the service stops with exit status 3. Read the worker log, then start the service again.

## Jobs on the recruiter sheet

Autopilot does not apply to a job on the [recruiter sheet](recruiter-sheet.md) unless you mark its row `apply_myself`. A job leaves the sheet when its company has no recent funding or its pay is low or unstated, and autopilot may then apply to it directly. The batch does not admit such a job, and it creates no queue row for it. A job already in the batch as **Queued** or **Resume Ready**, with no Applier, does not start and takes no slot. It still shows as **Queued** in the Dashboard. Mark it `apply_myself` and it starts in its normal turn.

This narrows what the batch does and starts nothing. A job an Applier already started is left alone. A stranded job on the sheet is not adopted, and the log shows `stranded_job_refused` with the reason `it is on the recruiter sheet`.

The hold is worked out from the sheet each time the worker reads it, at most once a second, and it is not stored anywhere. So the sheet and the hold cannot disagree, and nothing needs clearing when you choose `apply_myself`.

If the worker cannot read the sheet, autopilot admits nothing, starts no job that has not started, and adopts no stranded job. Appliers already working go on as usual. The worker log shows `recruiter_sheet_unavailable` once, and `recruiter_sheet_available` when reads work again. A broken `jobfeed/config.toml` or a missing `recruiter_sheet_rows` or `company_funding` table causes this. The dashboard creates those tables when it starts.

This hold covers the autopilot batch only. **Start with Claude** and other batches you select yourself still start the jobs you pick, including a job on the sheet.

## Jobs stranded by a dead worker

A worker can die with jobs still unfinished, as a reboot does. Those jobs stay **Applying** or **Resume Ready** and belong to an older batch that nothing runs. While autopilot is on, this is your standing authorization for the continuous batch to adopt them. You do not need to ask for a requeue. With autopilot off, nothing here runs.

The worker checks for stranded jobs before anything can start a Claude session, and then once a minute. The order matters. After a reboot, `claude agents` still lists the dead sessions as working, and the only proof that they ended is a background service status file from before the boot. The first new `claude --bg` rewrites that file. So every eligible job is adopted on the first check, whatever the free slots, and adopted jobs take the free slots before any new admission.

A job is left alone if it is held, already in this batch, or has a question or blocker still waiting on you. Otherwise it goes through the same `pipeline.requeue` checks as the command you run yourself. A job with submission evidence, an unknown or requested Submit, a Codex owner, or a session not shown to have ended is refused. A job whose posting is closed, incomplete, ignored or at an excluded company, or whose posting is already in the batch through another source, is not requeued.

An adopted job's Applier is retired, older batches stop listing it, and it starts again from the beginning in this batch. The requeue record names `autopilot` as the actor. Each refusal appears once per job and reason in the worker log as `stranded_job_refused`, and each adoption as `stranded_job_adopted`.

To put a job from this batch back in the queue yourself, for example after a resume update, stop `jobfeed-autopilot.service` and run `.venv/bin/python -m pipeline.cli requeue JOB_ID --reason WHY --keep-batches`. Without `--keep-batches` the job stays **Queued** and this batch never starts it again, and running the command again with the flag does not undo that.

## Install the units

Install them once, from the combined root:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/systemd/jobfeed-autopilot.service deploy/systemd/jobfeed-autopilot-cycle.service deploy/systemd/jobfeed-autopilot-cycle.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jobfeed-autopilot.service jobfeed-autopilot-cycle.timer
```

The units use the paths of the checkout at `~/jobfeed`. Edit them for another checkout. Check them with:

```sh
systemctl --user status jobfeed-autopilot.service jobfeed-autopilot-cycle.timer
systemctl --user list-timers jobfeed-autopilot-cycle.timer
journalctl --user -u jobfeed-autopilot.service -u jobfeed-autopilot-cycle.service
```

`systemctl --user stop jobfeed-autopilot.service` drains the worker and leaves its Appliers running. Stopping the units does not turn autopilot off, and turning autopilot off does not stop the units.
