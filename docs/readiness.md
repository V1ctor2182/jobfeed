# Check application readiness

1. Open **Workspace setup** in the combined dashboard. The same diagnostic is available in **Apply health**.
2. Select **Run readiness check**.
3. Wait for the component results. Local checks run first. A configured diagnostic relay then checks Chrome through the Codex harness.
4. Fix each **Blocked** component and run the check again. A required **Not tested** component also prevents an overall Ready result.

The diagnostic opens its own local test page, selects a generated synthetic PDF, and closes its own tab. It does not visit an employer, read a real resume into the browser, log in, create an application, or submit a form. Results expire after five minutes.

Codex may ask for browser-tool approval in the diagnostic chat. The dashboard remains Running while that approval is pending. Approve the localhost fixture test there to continue; this does not grant application authority.

**Refresh** updates saved application observations. It does not start another diagnostic. Repeated clicks and uncertain delivery retain the existing diagnostic request.

## Configure the diagnostic relay

Use a dedicated Codex Desktop chat in the combined `job-application-workspace/` project. Keep the pipeline runtime in `resume-application-pipeline/`; it does not need a separate project entry. This relay is separate from Claude inspection and batch bootstrap relays. Name it **Application readiness relay**. Give it an initial instruction to handle only readiness diagnostics, then wait for that turn to finish. Do not use an application controller or job chat.

Read the intended Chrome profile and extension instance from the official Codex browser inventory. Select the profile you intend to use for applications. Registration does not choose another profile when that connection is unavailable.

Set `PROJECT_ROOT` to the absolute combined folder and `APPLICATION_ROOT` to its nested `resume-application-pipeline/` folder. Set `CODEX_PROJECT_ID` to that combined project's ID and `DASHBOARD_ORIGIN` to the actual running dashboard origin. This local installation uses `http://127.0.0.1:8765`; verify it before registration. Run the command from `resume-application-pipeline/`, using the Python environment that contains its dependencies:

```sh
.venv/bin/python -m pipeline.application_readiness --root "$APPLICATION_ROOT" register \
  --project-id "$CODEX_PROJECT_ID" \
  --project-root "$PROJECT_ROOT" \
  --relay "$READINESS_RELAY_ID" \
  --extension-instance-id "$EXTENSION_INSTANCE_ID" \
  --profile-name "$CHROME_PROFILE_NAME" \
  --origin "$DASHBOARD_ORIGIN"
```

Use the same runtime selected by `JOBFEED_APPLY_ROOT`, or the dashboard's default sibling pipeline. Use the exact dashboard origin, including its port. Registration is local setup and does not grant application or submission authority.

To change the registered project root, profile, extension instance, relay, or origin, repeat the command with `--replace`. Replacement is allowed only after prior diagnostics have finished and their tab cleanup is verified. Uncertain delivery or cleanup prevents replacement.

If the result says delivery or cleanup is unknown, reconcile the original readiness chat. Do not create a replacement chat or repeat browser actions to clear the warning.

The relay reads frozen creation arguments from the diagnostic's private folder so embedded browser scripts reach the new chat unchanged. A verified preparation syntax error is recorded as Blocked only when native records prove that the original turn completed without executing a browser action. Other uncertain actions still require reconciliation.

## Read the result

Local checks cover the profile, existing PDF catalog, runtime dependencies, Autofill, database schema, diagnostic artifact directory, and Codex components. A live browser result also requires evidence from the original Codex diagnostic turn, the registered Chrome extension profile, the local page, synthetic file selection, and owned-tab cleanup.

Employer login, CAPTCHA, website permissions, required personal answers, and employer file acceptance remain checks for each authorized application. A global Ready result does not prove those conditions or grant permission to apply.

Claude resume sessions, Word, and the native tailoring helper are not required by the current existing-PDF application flow.

## Earlier verification on 2026-10-21

The built dashboard button was exercised against the real API adapter with a temporary database and an unregistered diagnostic runtime. It reported local results and the missing relay setup without dispatching a chat. Keyboard focus, console output, and layouts at 320, 768, 1024, and 1440 pixels were checked.

The exact browser probe also passed against a connected Chrome extension using an owned localhost fixture and a generated synthetic PDF. Original host tool records proved the registered browser identity, fixture marker, file selection, and tab closure. This verified the browser component. Live diagnostic relay dispatch was not activated; dispatch, concurrency, recovery, and provenance were tested with synthetic Desktop records.

Those earlier checks did not establish a registered relay for the current installation. Use the live checklist and a fresh diagnostic for current readiness. No real application or employer interaction ran during that verification.
