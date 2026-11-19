# Combined dashboard

Jobfeed is the shared dashboard for this workspace. The default Overview page combines discovery metrics with application queue and monitor observations. Apply health provides batch observations, job progress, queue filters, and infrastructure warnings.

Manually added job URLs carry **User priority** in the application queue. Apply health lists these rows first and offers a Priority filter. Priority remains separate from queue state and fit scores. It does not reopen completed jobs, interrupt existing work, or expand an approved batch. See [Use Jobfeed from a chat](jobfeed-mcp.md) to add URLs and prepare a plan.

The existing Triage, Library, Insights, Runs, Performance, Sources, and setup pages remain available. Overview, Apply health, and My Profile remain accessible when provider setup is incomplete.

## Edit your profile

Open **My Profile** from the sidebar or Workspace setup. The page at `/profile` brings personal details, résumé files, and job preferences together. Each section has its own save action and reports whether it saved successfully.

Personal details use the existing application profile. Blank fields mean that the answer is not recorded. Saving the form preserves other notes and job-specific answers. Credentials are not exposed in this editor. If another process changes the profile while the form is open, reload the saved details before trying again.

Uploading a résumé stores it locally and shows its extracted text. Saving preferences activates the uploaded file for scoring. Application PDFs are listed separately in the same section. Select **Use this PDF for applications** to add the uploaded PDF, then choose which listed PDFs are available. This does not start an application or upload anything to an employer.

Job preferences can be edited without AI analysis. Saving applies the supported location, company, and posting-age filters. Preferences used to generate searches may require a separate search review. Existing customized search URLs are retained until you edit them. The checklist links to any review still needed.

## Open the dashboard

Install the projects using the repository setup instructions. Build the frontend from the combined checkout:

```sh
cd jobfeed/web-ui
pnpm install --frozen-lockfile
pnpm build
cd ..
```

Use the Python environment where Jobfeed is installed to run `jobfeed serve`. With the environment created by the root README instructions:

```sh
.venv/bin/jobfeed --config config.toml serve --port 7654
```

Use your intended local configuration and database. `config.example.toml` is the checked-in reference. The dashboard is at [localhost:7654/dashboard](http://127.0.0.1:7654/dashboard). Application monitoring is at [localhost:7654/apply-health](http://127.0.0.1:7654/apply-health).

Opening the dashboard does not start application workers, import jobs, authorize submissions, or move runtime records from another checkout. Existing Jobfeed scan and evaluation actions remain on their original pages.

## Connect application observations

By default, application health reads `resume-application-pipeline/` beside the combined checkout's `jobfeed/` directory. It uses the configured Jobfeed SQLite database for the application queue and reads saved files under `monitor-output/` for batch observations.

To read an existing application runtime elsewhere, set an explicit absolute path when starting the web server:

```sh
JOBFEED_APPLY_ROOT=/absolute/path/to/resume-application-pipeline \
  .venv/bin/jobfeed --config config.toml serve --port 7654
```

Choose a Jobfeed configuration whose database belongs to that runtime. The dashboard does not automatically discover or switch to the original workspace. A source-only copy without runtime records shows missing monitoring or an uninitialized queue.

## Read health correctly

- Queue counts describe stored application states. They do not prove an agent is running.
- Batch progress comes from the last saved monitor observation. The page shows its timestamp and whether it is fresh, stale, missing, or unavailable.
- Submission evidence, Jobfeed synchronization, and tab cleanup remain separate measures.
- Batch counts can overlap because a job may appear in multiple batches. The dashboard does not add them together.
- A missing queue, missing monitor, unreadable file, or stale snapshot is distinct from a verified empty queue.
- Refresh fetches saved observations. It does not visit employer pages or perform a new application health check.

Both pages refresh periodically and offer a manual Refresh button. Filtering and opening row details are read-only. Worker control remains in the existing authorized application workflow.

**Run readiness check** is a separate action on Apply health. It checks local prerequisites and requests a harmless browser diagnostic through a dedicated Codex Desktop relay. It does not start application work. See [Check application readiness](readiness.md) for setup, results, and recovery.

## Data boundary

`GET /api/apply-health` reads the existing SQLite database in read-only mode and projects bounded local monitor files into an allowlisted response. It does not initialize queue tables, invoke the monitor, read agent transcripts, or expose application answers, authorization records, credentials, or raw submission evidence.

Jobfeed discovery metrics continue to use the existing insights API. The service's database reachability and application-monitor freshness are separate observations.

## Verification

Browser checks covered empty and synthetic populated data, queue search, job details, batch selection, and stale observations at widths of 320, 768, 1024, and 1440 pixels. The synthetic fixtures stayed in a temporary directory. No application workers were started and no real jobs were imported.

Backend tests exercise the production route, unchanged runtime files, source binding, freshness boundaries, malformed records, bounded reads, and overlapping batches. Frontend tests cover setup-independent monitoring routes, unavailable data, retained data after refresh failures, queue filters, and job details. The production assets are built into the existing tracked `jobfeed/web-ui/dist/` folder.

The original dashboard focused runs passed 62 backend tests and 70 frontend tests. The subsequent readiness work passed the full 221-test frontend suite, Python architecture and hygiene checks, TypeScript, ESLint, the design guard, generated-type consistency, and the production build. Vite reports the existing large main-bundle warning. See [readiness verification](readiness.md#verification-on-2026-10-21) for the button and Chrome diagnostic test boundaries.
