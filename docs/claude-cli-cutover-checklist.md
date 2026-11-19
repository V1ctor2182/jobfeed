# Run a batch with Claude Appliers

This checklist is for the user. It starts one reviewed batch with Claude CLI Appliers and can drain it. Claude Appliers are the only runtime. Nothing here starts on its own. The first real job still needs your exact go-ahead for that job. [Autopilot](autopilot.md), which only you turn on, runs its own continuous Claude batch.

Run the commands from `job-application-workspace/resume-application-pipeline` unless a step says otherwise.

## Before the first batch

1. Review and merge the branch. The work is on the local `claude-cli-phase1` branch in the `job-application-workspace-phase1` worktree. Merge it into `main` when you are satisfied, then remove the worktree with `git worktree remove ../job-application-workspace-phase1`.
2. Rebuild the Dashboard front end, so the Runs page shows Claude Appliers:

    ```sh
    cd ../jobfeed/web-ui && pnpm build
    ```

3. Restart the Dashboard so it loads the new pipeline code. It runs as the `jobfeed-dashboard` systemd user service, which starts at boot and logs to `jobfeed/data/jobfeed-serve.log`. A running worker survives the restart:

    ```sh
    systemctl --user restart jobfeed-dashboard
    ```

4. Make the `jobfeed` command reachable for submission sync. `finish_submission` runs `jobfeed/.venv/bin/jobfeed` if it exists and `jobfeed` on `PATH` otherwise. On this host only the root `.venv` exists, so start the worker with the root `.venv/bin` first on `PATH`, as in step 7.
5. Check that the Discord token file exists. The bridge runs only when `~/.config/jobfeed-applier/discord.env` is present.
6. Check that the Jobfeed extension points at `http://127.0.0.1:8765`.

## Run the first Claude batch

7. Prepare the batch the usual way, then start the worker with a local progress monitor. If an earlier drain is still in effect, `../.venv/bin/python -m pipeline.claude_cutover resume` lets new Appliers start again first:

    ```sh
    pgrep -f "pipeline.cli watch" || PATH="$(cd .. && pwd)/.venv/bin:$PATH" ../.venv/bin/python -u -m pipeline.cli watch --batch .pipeline/batches/<batch>.json --progress-output monitor-output/<batch> --progress-local-only
    ```

    The monitor writes `monitor-output/<batch>/monitor.json`, which gives each job its row in **Runs** with **Open application form** and the Submit button. It reads the Appliers' launch records.

    Or start from the Dashboard instead. In **Runs**, open the **Application queue** tab, select queued jobs, and click **Start with Claude**. It needs no Controller. It checks `application-profile.md`, `resume-catalog.json`, the `claude` and `jobfeed` commands, and the Discord token file, and lists everything missing at once. Then it writes `.pipeline/batches/dashboard-claude-<request>.json` and starts the same worker and monitor, with the root `.venv/bin` first on `PATH`. Its log is `monitor-output/dashboard-claude-<request>/worker.log`.

    Only one worker runs at a time. If a worker is already running, the Dashboard starts nothing and says so. Stop that worker first (one SIGINT, then wait for it to drain), then start the jobs again. The worker keeps running after the jobs settle, because Submit confirmations and Discord answers go through it. If it stops before the jobs finish, the request shows that. Start it again with the step 7 command, using `dashboard-claude-<request>` as `<batch>`.

    The worker restarts itself up to three times while a job still needs it, and manual restarts count toward the same three. After that it refuses with `restart_exhausted`. Find out why it keeps exiting in `worker.log`, make sure no worker is running, delete `monitor-output/<batch>/worker-restarts.json`, and start it again.

8. Answer questions and blockers in the job's thread in the **Jobfeed Appliers** Discord server.

    A CAPTCHA does not wait in a slot. When an Applier meets a CAPTCHA or bot check, such as reCAPTCHA, hCaptcha or a Cloudflare "Verify you are human" page, it reports blocker type `captcha` and ends its turn. No Applier tries to solve one. Within a minute the worker sets the job aside as **CAPTCHA needed** and starts the next eligible job in the freed slot. The job stays in the queue and in its batch, and nothing dispatches it while the hold lasts:

    - If the Applier never asked to Submit, its session ends and the job goes back to **Queued**, as `pipeline.cli requeue` does. Its folder stays.
    - If a Submit attempt is recorded, the click may have reached the employer. The session and its tab stay as they are, and the job is never requeued or filled again.

    The job's thread gets a **CAPTCHA needed** notice with the page link. Open the page in this computer's Chrome and complete the check yourself. Then press **I solved it, retry** on the notice, or run `../.venv/bin/python -m pipeline.cli captcha clear <job_id>`. A Queued job then starts again in its normal turn. A kept Applier is woken to check its tab again and takes a slot again. List the jobs on hold with `pipeline.cli captcha list`, or use the **Hold** filter in the Dashboard's **Application queue**.

    A lost form tab does not wait in a slot either. If the Applier's tab group disappears before any Submit attempt, the Applier reports blocker type `tab_missing` and ends its turn. Within a minute the worker puts the job back in **Queued** with no hold, still in its batch, and a new Applier fills the form again from the start in its normal turn. The job's thread says once that the tab was lost and nothing was sent. A Submit approval the old Applier never spent is withdrawn, and its Dashboard request is marked not submitted, so the new draft needs a new Submit; auto submit mode confirms it if it is on. After a recorded Submit attempt, the job keeps its session and asks you in its thread, as before.

    `claude agents` can still list an idle session as blocked after the Applier ended its turn. For a CAPTCHA or a lost tab before Submit, the worker stops such a session before it requeues the job. A session listed as working is left for a later pass.
9. Test the Dashboard jump on this first job. In **Runs**, click **Open application form** once while the Applier is running and once after it stops. The right tab should come forward both times.
10. Confirm Submit in the Dashboard as today, or reply `submit` to the review summary in Discord. The Applier is woken, clicks Submit once, and saves its evidence. The worker syncs the result to Jobfeed. Once Jobfeed records the job, its Discord thread shows the confirmation screenshot with the page link, once. A job confirmed by email gets no picture. A screenshot that is missing, over 8 MiB, or outside the job's folder is skipped, and the bridge log says why.

    Auto submit mode lets the system confirm Submit for you. Only you turn it on, and an agent never does:

    ```sh
    ../.venv/bin/python -m pipeline.cli auto-submit on      # or off, or status
    ```

    While it is on, the worker's Discord bridge confirms each new review summary through the same checks as the Dashboard, and posts in the job's thread that it did. Each confirmation covers one Submit attempt. A job with an open question or blocker, such as a CAPTCHA, an answer waiting for review, a Dashboard warning, a stopped Applier, or draining Appliers is left for you. Turning it off stops new confirmations; one already made still runs once. `pipeline.claude_cutover status` shows the mode.
11. Check the result in **Apply health**: the screenshot, the page text, and the applied status.

## Drain

12. Stop new launches and stop the Appliers waiting at a safe point. Run it again until `drain.working` is empty:

    ```sh
    ../.venv/bin/python -m pipeline.claude_cutover drain
    ```

    Every job keeps its owner and nothing is replayed. `pipeline.claude_cutover resume` lets new Appliers start again, and `pipeline.claude_cutover status` shows whether a drain is in effect. Resume one Applier later with `python -m pipeline.claude_applier --root . restart <launch_id>`.

## Still open after the switch

- A real Claude in Chrome extension disconnect has not been observed. Phase 1 and Phase 2 classify it, and each Applier checks its tab before every action.
- A Discord reply from a second account has only been unit-tested.
