---
name: setup-workspace
description: Set up this combined Jobfeed and resume application workspace for a first-time user. Install local dependencies, configure private profiles and existing PDFs, verify offline behavior, and optionally configure the dashboard or Claude MCP coordination. Use for onboarding or setup repair, not for starting applications.
---

# Set up the application workspace

Complete installation and report which parts are ready on this machine. Work in the combined checkout containing sibling `jobfeed/` and `resume-application-pipeline/` directories. Use this checkout as the single Codex and Claude project root. Resolve all paths from it, never from historical workstation examples. Keep the pipeline runtime root separate from the combined project root.

Setup permits local installation, configuration, and offline verification. A requested readiness check may use the isolated synthetic browser diagnostic described below. Setup does not authorize job imports, paid model calls, employer account creation, application dispatch, workers, resume uploads to employers, or submissions. Preserve existing services and unrelated registrations.

## Inspect the checkout

1. Locate the Git root and inspect its status. Preserve unrelated changes. If source is missing, use the repository URL supplied by the user. Do not guess a private remote or clone another copy over existing files.
2. Read root `AGENTS.md` and `README.md`. Read each child's `AGENTS.md` before working there.
3. Read `jobfeed/README.md` and the pipeline's `README.md`, `PRIVACY.md`, `GLOSSARY.md`, and `docs/pipeline.md`.
4. Check Python 3.11 or newer, Node.js with `node --test`, Git, Codex Desktop, its CLI, and the available official browser integration. Inspect actual installed versions. Record unavailable tools without claiming that Desktop or browser access works from installation alone.

The pipeline's `SETUP_PROMPT.md` describes a standalone checkout. In this combined repository, Jobfeed is already bundled at `jobfeed/`. Do not fetch a second Jobfeed project. Historical notes about installed scopes, model names, personal resumes, passing tests, or live relays describe another setup. Verify this machine independently.

## Install the local environments

Inspect the existing environments before installing. The batch bridge requires executable `resume-application-pipeline/.venv/bin/python` and `jobfeed/.venv/bin/jobfeed` paths. Reuse compatible existing child environments. A shared root environment can serve the dashboard and URL MCP when their commands explicitly use it, but does not replace those batch requirements. If child environments are missing, select an installed Python interpreter that meets the version requirement and run these commands from the Git root.

```sh
python3 -m venv jobfeed/.venv
jobfeed/.venv/bin/python -m pip install -e './jobfeed[dev]'
python3 -m venv resume-application-pipeline/.venv
resume-application-pipeline/.venv/bin/python -m pip install -r resume-application-pipeline/requirements.txt
```

Run the pipeline checks from `resume-application-pipeline/`.

```sh
.venv/bin/python tools/build_autofill_bridge.py
.venv/bin/python -m pipeline.cli --help
```

Run `jobfeed/.venv/bin/jobfeed --help` from the Git root. Do not invoke bare `jobfeed` as a help check, because it opens the local server and GUI.

Existing-PDF applications do not require Word, Claude resume-authoring sessions, document generation, or the native Claude dispatcher. Do not install those historical dependencies as setup prerequisites.

## Configure private data

Read `resume-application-pipeline/docs/existing-resumes.md` before creating the catalog. Preserve existing local files. Create missing files from these sources only when the user has supplied their real values.

| Source | Local destination |
|---|---|
| `jobfeed/config.example.toml` | `jobfeed/config.toml` |
| `jobfeed/resume.example.md` | `jobfeed/resume.md` |
| `resume-application-pipeline/examples/application-profile.example.md` | `resume-application-pipeline/application-profile.md` |
| `resume-application-pipeline/resume-catalog.example.json` | `resume-application-pipeline/resume-catalog.json` |
| `resume-application-pipeline/examples/application-bq-answer-library.example.md` | `resume-application-pipeline/docs/application-bq-answer-library.md`, if needed |

Collect missing inputs in one concise request. Ask for the intended database location, confirmed profile facts, approved existing PDF paths, and the desired model provider. Ask whether Claude coordination is needed only if the request does not establish it. Continue dependency installation and synthetic checks while waiting.

- Set Jobfeed's database explicitly. Default to a new checkout-local `jobfeed/data/jobfeed.sqlite` for a fresh installation. Ask before using or migrating another installation's database. Inspect relevant environment overrides without exposing credentials, because they can override `config.toml`.
- Set `llm.master_resume_path` to the user's real `resume.md` when configuring real scoring. The synthetic `resume.example.md` cannot represent the user.
- Configure provider and model choices supported by the installed runtime. Do not treat example model IDs, prices, or historical subscriptions as current facts. Keep secrets in the documented local secret mechanism or environment, never in tracked config or output. Defer billable provider tests unless authorized.
- Explicitly list only user-approved PDFs in `resume-catalog.json`. Set `schema_version: 1` and mark each approved usable entry with `available: true`; an entry without that flag cannot be selected. Resolve relative paths against that catalog. Verify each available file exists, opens as a PDF, and contains no unresolved placeholders or private annotation pages. Do not scan a directory and enable every PDF.
- Use optional `text` and `autofill` companions only when they match the selected PDF. Without matching companions, retain the documented upload-only metadata behavior.
- Populate profile and BQ answers only from confirmed facts. Do not copy synthetic examples as real education, experience, eligibility, or personal stories. Leave missing facts visibly unresolved and report the affected capability as blocked.

Check every created private path with `git check-ignore -v`. Check `git status --short --untracked-files=all` for accidental personal data. Add a narrowly scoped ignore rule before creating any private file that is not already ignored. Do not force-add private files. Keep credentials out of validation logs.

## Make application skills available

Confirm the Codex project can discover the pipeline's bundled skills.

- `resume-application-pipeline/.agents/skills/application-execution/SKILL.md`.
- `resume-application-pipeline/.agents/skills/start-pipeline/SKILL.md`.
- `resume-application-pipeline/.agents/skills/application-question/SKILL.md`.

Create relay and application chats in the combined Codex project. Read the nested pipeline instructions explicitly and resolve the skill paths above against the combined root when the host does not discover nested skills. Run pipeline commands from the nested runtime directory with its environment. Do not require a second saved project or move the project root into the pipeline folder.

BQ is bundled in `application-question`. Do not request another BQ repository. Set `BQ_PROJECT` only for an intentional compatible override. Offline bridge tests do not establish live research access.

## Verify offline behavior

Run the documented synthetic checks from `resume-application-pipeline/`.

```sh
.venv/bin/python tools/create_example_template.py
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m unittest discover -s .agents/skills/application-question/tests -p 'test_*.py'
node --test tests/*.mjs autofill/bridge/*.test.mjs
```

Run Jobfeed's default test suite from `jobfeed/` with `.venv/bin/python -m pytest`, as defined by its `Makefile`. Keep fixtures in temporary storage and use a separate synthetic database. Do not point tests at the user's live database. If URL-intake tests need the optional MCP dependencies, use the environment described in `docs/jobfeed-url-mcp.md` rather than hiding their import failures.

Distinguish dependency or sandbox failures from existing regressions. Do not delete tests, weaken authorization, or broaden setup into orchestration changes to obtain a passing result. Report counts from this run, not historical counts.

## Complete the combined checklist

For the dashboard, follow root `docs/dashboard.md`. Prebuilt assets are tracked. If rebuilding is needed, use the `packageManager` version in `jobfeed/web-ui/package.json` and run `pnpm install --frozen-lockfile`, then `pnpm build` from `jobfeed/web-ui/`.

Reuse the actual running dashboard origin and configuration. Check its process or saved launcher before choosing a port. The current local installation uses `http://127.0.0.1:8765`; verify that value on each machine. When local dashboard startup is in scope, set `DASHBOARD_PORT` to the chosen port and run this from `jobfeed/` with the intended configuration.

```sh
.venv/bin/jobfeed --config config.toml serve --host 127.0.0.1 --port "$DASHBOARD_PORT"
```

Check an occupied port before starting another server. Open `/setup` for the combined checklist, `/dashboard` for jobs, and `/apply-health` for observations on the actual loopback server. An empty or missing queue is expected in a source-only checkout. Saved observations do not prove that workers are active. Use `JOBFEED_APPLY_ROOT` only for an explicitly selected external pipeline runtime with its matching database. Do not trigger scans or evaluation as setup smoke tests.

Verify each capability separately. A saved provider configuration does not prove the whole workspace is ready.

1. Save the user's actual Jobfeed resume and provider settings, then read back the active configuration. A resume left only in the onboarding draft is not active scoring input. Report any field validation error before declaring the save complete.
2. Verify URL intake through the registered MCP's `capabilities` tool. Confirm that it uses the same database as the dashboard. Job import does not depend on a browser readiness result and does not start applications.
3. Verify application prerequisites and the diagnostic relay through root `docs/readiness.md`. Confirm the actual profile, explicitly available PDF catalog, child environments, and browser binding. Register a dedicated readiness relay only within the user's authorized setup scope. Use the combined Codex project ID and `--project-root` for registration; keep `--root` pointed at the nested pipeline. Read the profile and extension identity from the official browser inventory, not from historical records. A requested readiness check opens only its own synthetic localhost page. It is not an employer application. An expired result requires a new check before claiming current browser readiness.
4. Check optional Claude batch coordination separately. A readiness relay, an inspection relay, and a batch bootstrap relay have different roles. Do not reuse one chat across those roles. Registration and a successful check do not authorize a batch.

Configure Claude integrations only when requested or already included in the user's setup request. Read the matching pipeline guide and generate entries using this checkout's absolute paths and installed environments.

- `docs/claude-codex-bridge.md` for read-only Desktop inspection.
- `docs/claude-batch-bridge.md` for reviewed batch coordination.
- `docs/jobfeed-url-mcp.md` for URL-intake MCP and its separate dependencies.

A project `.mcp.json` registers URL intake for Claude Code opened in that project. It does not register the server in Claude Desktop. For Desktop access, merge the required server entry into the actual Desktop configuration after backing it up, preserve unrelated entries, and verify the client's loaded tools after reload. An inspection bridge alone does not expose `add_job_urls`.

Keep batch scopes unarmed. Use the exact dashboard origin as the batch registration's `api_url`, and confirm that its database matches the dashboard. A live-capable batch scope also needs the verified combined project ID, its absolute `project_root`, a dedicated bootstrap relay ID, and an absolute Codex database path. The batch bridge's `--root` still points at the nested pipeline runtime. Inspect `batch_capabilities` for missing requirements. Saved batch registrations are immutable. If origin, runtime, relay, or other bound settings change, register a fresh scope and update the MCP entry to that scope. Do not rewrite an old scope or transfer its plans or grants.

Scope registration, a connected MCP server, a registered Desktop relay, and exact-plan approval are separate checks. Verify capabilities without importing jobs or starting a batch. Respect the host's trust and tool approval prompts.

Initialize an empty database only at the selected fresh path using the documented initialization command. Never initialize over an existing database. Do not copy the synthetic legacy fixture into runtime storage.

## Report the result

Report installed versions, configured file locations, actual verification results, and unresolved inputs without exposing private values. Report checklist results separately for URL intake, discovery and scoring, application readiness, and optional Claude batch coordination. Include the active resume path, actual dashboard origin, selected database path, and readiness timestamp without copying resume content or credentials. Include the exact local startup command and working directory for the chosen checkout. State whether a dashboard process remains running.

End with the remaining steps needed before a real batch. Setup does not supply job scope, concurrency, target count, model choices, or final-submit authorization. Those come from the user's current application request.
