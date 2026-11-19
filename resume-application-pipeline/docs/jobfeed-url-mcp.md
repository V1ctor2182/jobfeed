# Add job URLs from Codex or Claude

The `jobfeed` MCP imports a public posting into Jobfeed and admits the saved job to the application queue with user priority. It reuses Jobfeed's ATS adapters, source IDs, canonical identity resolver, and SQLite store. Queue admission belongs to the application pipeline. The two repositories communicate through a bounded JSON command so each can use its own Python environment. See [Use Jobfeed from a chat](../../docs/jobfeed-mcp.md) for the combined workflow and client connections.

## Install

Run these commands from the parent folder that contains `jobfeed` and `resume-application-pipeline`. Python 3.11 or newer is required.

```sh
python3 -m venv .venv-jobfeed-mcp
.venv-jobfeed-mcp/bin/python -m pip install -r resume-application-pipeline/requirements-jobfeed-mcp.txt
```

For an existing Jobfeed installation, use its actual SQLite path. Both the importer and queue must use the same database. The server reports a missing database instead of silently creating another queue.

For a new installation, explicitly initialize an empty database.

```sh
PYTHONPATH=jobfeed/src .venv-jobfeed-mcp/bin/python -m jobfeed.import_url \
  --db /absolute/path/to/job-application-workspace/jobfeed/data/jobfeed.sqlite --initialize
```

Adjust the absolute paths below if the checkouts are elsewhere. Add this entry to the parent project's `.mcp.json`, preserving other servers.

```json
{
  "mcpServers": {
    "jobfeed": {
      "command": "/absolute/path/to/job-application-workspace/.venv-jobfeed-mcp/bin/python",
      "args": [
        "-m", "pipeline.jobfeed_intake_mcp",
        "--db", "/absolute/path/to/job-application-workspace/jobfeed/data/jobfeed.sqlite",
        "--jobfeed-root", "/absolute/path/to/job-application-workspace/jobfeed",
        "--jobfeed-python", "/absolute/path/to/job-application-workspace/.venv-jobfeed-mcp/bin/python"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/job-application-workspace/resume-application-pipeline"
      }
    }
  }
}
```

Open Claude Code in `/absolute/path/to/job-application-workspace` and approve this project MCP when Claude presents its trust prompt. Call `capabilities` to check connection and database availability without importing a job.

Ask Claude to add a specific job URL to Jobfeed. The tool call is `add_job_url({"url":"https://boards.greenhouse.io/acme/jobs/123"})`. This URL illustrates the shape; use a real posting when importing.

## Add a list

Call `add_job_urls` with 1 to 20 URL strings. Each batch runs up to three imports at once and uses the same import and source-specific queue admission path as `add_job_url`. A full batch of slow pages can take several minutes.

```json
{
  "urls": [
    "https://boards.greenhouse.io/acme/jobs/123",
    "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111"
  ]
}
```

Use real posting URLs in place of these examples. Invalid containers, empty lists, lists with non-string entries, and lists over 20 items are rejected before any import starts. A malformed URL string receives an individual result, so other valid URLs can still be imported.

The response preserves input order. Each entry has its original `index` and `url`, plus a nested `result` containing the same fields as `add_job_url`. For example, a repeated URL can return this result.

```json
{
  "status": "completed",
  "results": [
    {"index": 0, "url": "https://boards.greenhouse.io/acme/jobs/123", "result": {"status": "queued", "source_job_id": 1, "real_job_id": 1, "queue_job_id": 1, "queue_state": "Queued", "imported": true}},
    {"index": 1, "url": "https://boards.greenhouse.io/acme/jobs/123", "result": {"status": "already_queued", "source_job_id": 1, "real_job_id": 1, "queue_job_id": 1, "queue_state": "Queued", "imported": false}}
  ],
  "counts_by_status": {"queued": 1, "already_queued": 1},
  "newly_queued": 1
}
```

`counts_by_status` counts input outcomes, including repeated inputs. `newly_queued` counts only committed new queue entries. Concurrent imports can finish in any order, so a later repeated input can be the entry that reports `queued`. Before starting a batch, the pipeline asks Jobfeed's read-only `--url-keys` command to normalize its inputs. This command does not read pages or write jobs. Inputs with the same normalized URL or known native posting identity wait on one batch-local lock before using an import slot. Each waiter then runs a fresh source lookup and queue admission, so successful first imports prevent repeated page fetches without caching a `queued` response. Different URLs can still use all three import slots. Separate batches and independent MCP processes using the same resolved database path also coordinate each URL import through a persistent lock. A waiter acquires ownership and rechecks saved source data before deciding whether to fetch.

A batch does not roll back earlier successes when another URL fails or the call is cancelled. Cancellation stops and awaits active importer processes and prevents waiting items from starting. Saved jobs and committed queue entries remain. Retry the same list to finish interrupted work without resetting existing queue states. This tool starts no application workers and grants no submission permission.

## Supported postings

- Greenhouse job pages on `boards.greenhouse.io` and `job-boards.greenhouse.io` use the exact job API.
- Ashby and Lever posting pages use the existing board API and select the exact native ID.
- Other public pages must contain exactly one `JobPosting` JSON-LD record with a real title, hiring organization, and description. If the record supplies a URL, it must match the fetched page.

Search pages, login pages, missing metadata, expired postings, ambiguous exact-URL matches, and private URLs cannot create new queue entries. The importer does not invent descriptions or employer names. Native adapters retain their existing employer board slug fallback when the API has no display name.

URLs and page content are untrusted data. Only bounded public GET requests are permitted. Redirects receive the same URL validation, with at most five redirects, a 2 MB decoded response limit, and a 40 second import budget. No browser, credentials, or arbitrary command tool is exposed.

## Results and retries

A result returns `source_job_id`, `real_job_id`, `queue_job_id`, and `queue_state` when those values exist. `queue_job_id` identifies that source posting. `manual_priority` records user selection independently of the queue state. Repeated URLs promote existing rows without reopening or resetting them. `identity_warnings` lists advisory flags, and `related_source_job_ids` lists other sources grouped under the same canonical job. These fields are returned on repeat admission too.

| Status | Meaning |
| --- | --- |
| `queued` | A new queue entry was committed. |
| `already_queued` | An entry already exists. Read `queue_state`; completed, cancelled, and other terminal entries remain unchanged. |
| `already_applied` | A receipt for this exact source prevents another entry. |
| `closed` | This source posting is closed. |
| `identity_review` | The importer found an ambiguous exact-URL or native-posting match and cannot safely select a source. |
| `needs_details` | Import or admission cannot proceed. Read `reason`, which can identify unsupported content, missing metadata, or an explicitly ignored job. |
| `imported_not_queued` | Jobfeed saved the posting but queue admission failed. Saved IDs are returned. Repair the queue and retry the URL. |
| `error` | Fetch, configuration, or process failure. A process timeout can leave the save outcome unknown; retry the same URL after resolving the error. |

Jobfeed checks saved source URLs, saved application URLs, and their recorded native URL evidence before requesting a page. It normalizes both incoming and saved URLs, including tracking parameters. A known source with title, company, description, and URL is reused without page fetching or parsing. If required metadata is missing, the importer fetches the posting to fill the missing fields while preserving existing required metadata. URL matching does not select arbitrary canonical siblings. Canonical identity matching remains in the store, and queue eligibility remains in queue admission.

Repeated requests and tracking aliases converge through existing native source IDs and Jobfeed's canonical resolver. Shared queue admission uses `BEGIN IMMEDIATE` to check this source's queue state and applied receipts before inserting. Independent processes therefore cannot admit two entries for the same source. Distinct application portals can each enter the queue, even when they share one canonical job. All existing queue states remain unchanged on retry.

Cross-source matches are advisory. `related_sources`, `related_source_queued`, and `related_source_applied` flag grouped postings and their existing work. `canonical_application_recorded` and `canonical_status_applied` flag canonical history without treating this source as submitted. `identity_review:<state>` flags unresolved canonical identity, and `canonical_closed` flags a closed parent when this source remains open. Existing sibling state is never reset. An explicitly ignored job, missing canonical record, closed source, or incomplete source still prevents new admission.

Receipt readback is source-specific throughout the pipeline. An application through portal A does not mark portal B applied. The normal discovery worker still selects from Jobfeed's canonical, scored Results API, which returns representative sources and retains its own selection policy. User-priority rows sort first among eligible candidates. For exact manually imported sources, the batch MCP's `prepare_manual_batch` accepts their source job IDs without requiring a fit verdict. It validates the posting, exclusions, current queue state, and source application history before freezing a plan for human approval. Identity review state does not change a frozen Results snapshot. Advisory flags remain available in MCP intake and queue admission.

If a later identity merge combines two jobs that were distinct when queued, their historical entries can both remain. Intake does not delete or rewrite active work to conceal such a merge.

Importing does not run fit evaluation, launch application workers, fill forms, or grant submission permission. The queue remains available for the normal application workflow.

## Coordination across MCP processes

For each database, Jobfeed creates a sidecar directory named `.<database filename>.url-import` beside the resolved database file. Its `slots.sqlite` registry stores complete URL keys and stable integer slots. Numbered files such as `1.lock` provide POSIX `flock` ownership. URL text never becomes a filename, and no hashes are calculated.

The lock covers source lookup, fetching, saving, and readback. Registry transactions finish before waiting or fetching, and no main Jobfeed database write transaction stays open during HTTP requests. Different keys can proceed concurrently. Each caller separately performs source-specific queue admission after the importer returns.

A successful owner saves the source before releasing its lock. Waiting callers then reuse that source without fetching again. Cancellation or process death closes the lock descriptor, allowing another caller to proceed. The existing 40 second import budget includes time spent waiting; a timeout returns a retryable error without bypassing the lock.

This coordination supports POSIX systems with reliable local filesystem locks, including this macOS workspace. Use the same database path or a symlink that resolves to it. Hard-linked database aliases and network filesystems are outside this guarantee.

The registry and numbered files remain on disk after imports. Never delete or replace them while any importer is running, because replacing a lock file can allow two owners to hold different inodes. They may be removed together for offline cleanup after all importers stop. An unreadable or corrupt registry fails the import instead of bypassing coordination.

This is not an exactly-once HTTP guarantee after a crash. If the owner dies after fetching but before saving, the next caller can fetch again. If saving completed, the next caller reuses the saved source. Failed or incomplete imports can be retried sequentially. Unknown redirect aliases can initially have different keys; source lookup still reuses proven URL matches after discovery.

## Verify changes

Install test dependencies in the intake environment, then run the focused suite from the parent folder.

```sh
.venv-jobfeed-mcp/bin/python -m pip install pytest pytest-asyncio
PYTHONPATH=jobfeed/src:resume-application-pipeline \
  .venv-jobfeed-mcp/bin/python -m pytest -q \
  resume-application-pipeline/tests/test_jobfeed_intake.py \
  resume-application-pipeline/tests/test_jobfeed_import_coordination.py \
  resume-application-pipeline/tests/test_queue_fresh_dispatch.py \
  resume-application-pipeline/tests/test_queue_integration_v2.py \
  resume-application-pipeline/tests/test_advice_store.py \
  resume-application-pipeline/tests/test_pipeline.py
```

The intake suite uses temporary real Jobfeed databases, controlled HTTP fixtures, independent queue processes, and the official MCP client against an actual stdio server. Batch tests cover multiple distinct jobs, mixed results, overlapping lists, ordered output, input limits, and cancellation that reaps active subprocesses. Request counters verify zero fetches for known complete sources and one successful fetch shared by independent MCP server processes. Coordination tests also exercise different-key overlap, process death, cancellation, retry after failure, waiting deadlines, and database symlinks. It covers repeated imports, canonical siblings, receipts, terminal states, closed jobs, workflow exclusions, partial failure recovery, response compression, and public URL restrictions. It starts no applications and writes no test jobs into the configured user database.
