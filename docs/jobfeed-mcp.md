# Use Jobfeed from a chat

## Claude Code: local commands by default

Open Claude Code in the combined `job-application-workspace` folder. Its root
[CLAUDE.md](../CLAUDE.md) tells Claude to queue exact job URLs as User priority
using the local intake command. Paste one URL or a list. No MCP connection is
required, and URL intake does not start applications. Start a fresh session to
load the new instructions, or ask an existing session to read `CLAUDE.md` first.

The command uses the existing intake service and the database configured in
`jobfeed/config.toml`. From the combined root, with a JSON array of URLs saved
in a file:

```sh
PYTHONPATH=resume-application-pipeline .venv/bin/python -m pipeline.jobfeed_intake_cli --file /absolute/path/to/urls.json
```

The command also accepts JSON on stdin with `--file -` or positional URL
arguments. It accepts at most 20 URLs per call; Claude splits longer lists into
sequential groups. Results identify each URL's outcome, including duplicates
and partial failures. Missing databases are reported rather than created.

## Discord: post URLs in the channel

Post one or more job posting URLs as an ordinary message in the bot's Discord
channel. The bot queues them with User priority through the same validation and
intake as the command above, then replies with each URL's outcome and job ID. It
reacts ✅ when every URL is saved and ⚠️ when any is not. It only queues. It
never starts applications, workers, or browser sessions.

The `jobfeed-discord-intake` systemd user service runs it, independent of any
Applier worker. It reads `~/.config/jobfeed-applier/discord.env`, polls the
channel set by `DISCORD_INTAKE_CHANNEL_ID`, or else the Applier bridge's main
channel, and accepts messages only from the bridge's allowed senders. Thread
messages, bots, and messages without a URL are ignored. It logs JSON events to
`resume-application-pipeline/monitor-output/discord-intake.log` and keeps its
cursor and handled messages in `resume-application-pipeline/.pipeline/discord-intake.sqlite3`.
On its first start it skips the channel's history.

```sh
systemctl --user status jobfeed-discord-intake
systemctl --user restart jobfeed-discord-intake
```

## Optional MCP connections

For clients using MCP, enable the combined project's connections, then ask the agent to call `jobfeed.capabilities` and `codex_application_batches.batch_capabilities`. Capability calls inspect setup without importing jobs or starting applications.

Local Codex reads the trusted project's `.codex/config.toml`. Claude Code reads the project's `.mcp.json`. Claude Desktop needs the same server commands registered separately in its own configuration. A browser-only cloud chat cannot use these local process commands directly. These files contain machine-specific paths and remain ignored by Git.

After adding or changing a connection, reload the client's MCP connections and verify that the tools appear. A configuration file alone does not prove the current chat has loaded the tools.

## Add your chosen postings

Paste exact public job posting URLs with this instruction.

> Add these URLs to Jobfeed as my priorities. Report each saved job ID, duplicates, and failures. Do not start applications.

The intake MCP provides `add_job_url` for one URL and `add_job_urls` for lists of up to 20. Ask the agent to split longer lists. Company names, role names, and search-results pages are not accepted as job postings. Supported pages are Greenhouse, Ashby, Lever, and public pages with one complete JobPosting JSON-LD record.

Manually supplied postings receive **User priority**, separate from fit scores and application state. Repeating a URL reuses the existing source and promotes its priority without resetting its queue state. Terminal or already-applied jobs are not reopened. Related postings on different application portals retain their existing advisory flags.

Open **Apply health** to see the queue and its Priority column. Filter for **User priority** to find your selections. Priority affects eligible work within the authorized scope. It does not interrupt active work, expand a reviewed batch, or grant permission to submit.

## Prepare an application plan

After reviewing the imported jobs, ask the agent to prepare a plan for their exact source job IDs. `prepare_manual_batch` accepts explicitly selected manual jobs without requiring an AI fit verdict. It still checks the saved posting, exclusions, current queue state, and application history. For scored discovery results, use `list_batch_candidates` followed by `prepare_batch`.

Review the proposed jobs, résumé, concurrency, and submission policy. New plans put user-priority jobs first. Once a plan is frozen for review, its membership and order stay fixed. Starting it is a separate action and requires the existing batch authorization process. `get_batch` reports status, and `pause_batch` requests a pause of an existing batch.

Adding URLs or preparing a plan does not start applications. Saving for later is the default for pasted URLs; starting applications requires separate authorization.
