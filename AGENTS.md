# Combined application workspace

This repository contains two sibling projects. Follow each project's AGENTS.md before editing its files.

Keep `jobfeed/` and `resume-application-pipeline/` side by side. Pipeline discovery and the formal Jobfeed CLI use this layout.

Do not commit personal resumes, profiles, credentials, local configuration, application records, runtime databases, or logs. The tracked Jobfeed legacy database fixture is synthetic test data.

Setup and tests do not authorize real applications. Preserve the existing application authorization boundaries. Copying this repository does not migrate or start local services, workers, browser sessions, or Claude MCP registrations.

The server setup prompt is written once, in `docs/setup-prompt.txt`. After editing it, run `python3 scripts/sync_setup_prompt.py` to update the README and the website's copy button; CI fails while they differ.
