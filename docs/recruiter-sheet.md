# Recruiter sheet

The recruiter sheet lists the New York jobs at good companies worth asking your recruiters about. A good company has recent funding and good pay. Autopilot does not apply to these jobs, because a direct application could conflict with a recruiter putting you forward. Nothing here contacts a recruiter.

Jobfeed works the sheet out from existing data on every read, so a new evaluation appears with no job or timer. The Jobfeed API serves it, and there is no Dashboard page. You read and edit it in a Google Sheet, which the dashboard syncs with Jobfeed at the end of every evaluation.

## Which jobs qualify

A job qualifies when all of these hold:

- Its Stage B verdict is **apply**.
- Its location matches `locations`.
- Its fit score is at least `min_fit`.
- It has no critical gap and at most `max_major_gaps` major gaps.
- Its title is not an internship or co-op.
- Its company is not in `excluded_companies` or the brands the pipeline already excludes.
- It is not closed, ignored or applied to.
- It has no queue row, or only Queued rows with no owner.
- Its pay is at least `min_pay`. Pay is the midpoint of the range stated in the posting. A posting with no stated range fails, because New York law requires one.
- Its company's last funding round was announced within `funding_within_months` of today, or the company has no funding record yet.

**A job that leaves the sheet is no longer held.** Autopilot may then apply to it directly, once it has its resume. That includes a job at a company with no recent funding, and a job with low or unstated pay.

A job stays on the sheet for as long as it qualifies. Once you edit its row, it stays even if it stops qualifying, so a job a recruiter is handling never vanishes. A closed job then stays visible with `closed` set. A job you never edited leaves when it stops qualifying.

## Configuration

Add this to `jobfeed/config.toml`. A file with no such section uses these defaults, with no excluded companies.

```toml
[recruiter_sheet]
min_fit = 78
max_major_gaps = 2
excluded_companies = ["Digital Waffle", "CyberCoders"]
min_pay = 130000
funding_within_months = 12
```

`excluded_companies` matches any part of the company name, ignoring case. It only keeps a company off the sheet, and autopilot may still apply to that company directly.

`min_pay` compares with the yearly midpoint of a posting's range in dollars. `funding_within_months` counts calendar months back from today, including that day.

`locations` is a list of case-insensitive regular expressions, and setting it replaces the whole default in `jobfeed/src/jobfeed/domain/recruiter_sheet.py`. The default means New York City. Buffalo, Monroe, Great Neck and "Albany, New York" do not match. A multi-city posting that lists NYC does match. A location that says only "NY", or that ends in a bare ", New York", does not match, because it could be anywhere in the state.

Jobfeed and the worker read this section from the file on every read, so a change needs no restart.

## The funding check

Each company on the sheet needs a funding record, kept in the `company_funding` table and keyed by the company's normalized name. Code decides whether a round is recent. The model only reports the round.

| What the record says | What happens to the company's jobs |
| --- | --- |
| No record yet | They stay on the sheet and held, with `funding_pending` true. The check has not run, so autopilot must not apply yet. |
| A round inside the window | They stay on the sheet and held. |
| A round outside the window, or a future date | They leave the sheet unless you edited the row. |
| No round found, and no round ever stored | They leave the sheet unless you edited the row. |

A job that fails another rule gets no lookup and shows no pending state, because funding cannot change its result.

### Which companies get a lookup

The sync looks up a company when one of its jobs passes every other rule and the company has no record or a record over 30 days old. It takes the best fit first. It makes up to 10 lookups per run. It starts none after 10 minutes or after 3 errors in a row, and it logs when errors stop it. An older record stays in force until a new check replaces it, and a failed lookup changes nothing and retries on the next run.

A lookup never loses a stored round. A later check that finds no round, or an earlier round, keeps the stored round and updates only the check time, so a flaky search cannot release a job. A round with the same date or a later one replaces the stored round, and any round replaces a stored `None found`. The stored round still ages out of the window as usual.

### Forget a wrong round

A lookup never replaces a stored round with a worse answer, so a wrong round stays until you remove it. To remove one, run this from `jobfeed/`:

```sh
../.venv/bin/python -m jobfeed.recruiter_sheet_sync --config config.toml --forget-funding "Cerberus Capital Management"
```

The command normalizes the name, deletes that company's record, prints whether one existed, and exits without syncing. It refuses a name with no letters or digits. The company's jobs are then pending and held, and the next sync looks the company up again.

### What one lookup does

One lookup is one `claude -p` run on the Haiku model, which allows web search and no other tool. It costs about $0.025 and takes 11 to 20 seconds, as measured on real companies. A run may cost at most $0.25 and take at most 170 seconds. The run's first event must list no tool other than web search and the structured answer, and no MCP server. Otherwise the sync refuses the result.

The prompt holds today's date, the company, the job title, and up to 3 sentences of the posting that mention funding. Each sentence is cut to 220 characters and labeled as data.

Only a round the company itself raised counts. A round it invested in or led as an investor does not, and neither does one raised by a subsidiary, a portfolio company, or a similarly named company. A Series A raised by Star Catcher Industries and co-led by Cerberus Ventures is not funding for Cerberus Capital Management.

Code checks an answer that says a round was found:

- `raised_by` names the company. Code splits both names into words with `normalize_company_name`, and one set of words must contain the other. `Blossom` matches `Blossom Health`. `Star Catcher Industries` and `Cerberus Ventures` do not match `Cerberus Capital Management`.
- `source_url` is https, and that exact URL appears in the raw web search results.
- `announced_on` is `YYYY-MM-DD` or `YYYY-MM`, and it is not in the future. A month means its first day.
- `round` is not blank.
- `amount_usd` is a positive number or null.

A `raised_by` that names another company counts as no round found, because the answer says someone else raised it. A missing or blank `raised_by` is an error, as is an answer that fails any other check. On an error the sync stores nothing and retries on the next run. The sync does not store `raised_by`. An answer that says no round was found is stored as a real result, unless a round is already stored.

## Statuses

A row with no stored status is `to_ask`.

| Status | Autopilot | May change to |
| --- | --- | --- |
| `to_ask` | Holds it | `asked`, `with_recruiter`, `passed`, `apply_myself` |
| `asked` | Holds it | `to_ask`, `with_recruiter`, `passed`, `apply_myself` |
| `with_recruiter` | Holds it | `asked`, `passed`, `apply_myself` |
| `passed` | Holds it | `to_ask`, `asked`, `apply_myself` |
| `apply_myself` | May apply | `to_ask`, `asked`, `passed` |

## API

These routes are under `/api` on the dashboard.

| Route | What it does |
| --- | --- |
| `GET /recruiter-sheet` | Returns `as_of` and every row, best fit first. Cell text is as stored, not neutralized. Each row has `pay`, `funding`, `funding_source`, and `funding_pending`. |
| `GET /recruiter-sheet/statuses` | Returns the allowed statuses, the transitions, and each row's `status` and `status_updated_at`. |
| `GET /recruiter-sheet.csv` | Returns the sheet as CSV with spreadsheet formulas neutralized. It ends with the `pay`, `funding`, `funding_source`, and `funding_pending` columns. |
| `PUT /recruiter-sheet/{real_job_id}` | Saves one row's `status`, and `recruiter` and `note` if sent. An omitted field keeps its value. |
| `POST /recruiter-sheet/edits` | Applies up to 500 edits, `{"edits": [{"real_job_id", "status", "recruiter", "note"}]}`. |

`real_job_id` is the stable key of a row, and it is the first CSV column. In Python, `jobfeed.recruiter_sheet.read_sheet` and `apply_edits` do the same work as the routes.

Both write routes refuse a request unless its `Origin` header is the dashboard's own origin. A script sends `Origin: http://127.0.0.1:8765`.

A batch reports one result per edit, with `outcome` set to `applied`, `unchanged` or `rejected`. A rejected edit has a `code`, which is `not_on_sheet`, `unknown_status`, `invalid_transition` or `too_long`, and the other edits still save. An edit that changes nothing reports `unchanged`, keeps the row's timestamp and stores nothing, so sending a whole sheet back marks no row changed and pins no job to the sheet.

`funding` is display text such as `Series A, $17M, 2026-03-17`, `None found`, or blank when the company has no record. `funding_source` is the round's URL.

`status_updated_at` is the time you last changed that row, and it is null until then. `updated_at` is the later of that time and the job's last evaluation.

Postings can contain text such as `=HYPERLINK(...)`. The CSV prefixes such a cell with an apostrophe. A client that writes the JSON into a spreadsheet must do the same or write values raw.

## Sync with the Google Sheet

The dashboard syncs the sheet as the last step of every evaluation it runs, including the daily autopilot evaluation. It syncs after a failed or stopped evaluation too, because such a run may still have saved evaluations. It does not sync after a dry run. The evaluation's result does not depend on the sync, and a new evaluation can start while the sync runs. Edits you make in the Google Sheet reach Jobfeed at the next sync. To save them sooner, run the command below.

`python -m jobfeed.recruiter_sheet_sync --config config.toml` runs one sync by hand and exits. A sync waits for any sync already running to finish, so two never overlap and none is skipped. `--dry-run` reads the sheet and reports what it would do, making no lookup, saving no edit, and writing no cell. `--forget-funding` is described in [Forget a wrong round](#forget-a-wrong-round). Set the sheet in `jobfeed/config.toml`. An empty `google_sheet_id` makes the sync do nothing and say so, so no lookup runs and every company stays pending.

```toml
[recruiter_sheet]
google_sheet_id = "the long ID in the sheet's URL"
google_sheet_tab_id = 0  # the gid of the Jobs tab
```

The sync runs while autopilot is off, and it starts no applications.

### Who owns which column

The tab is named `Jobs`, and its first row must be exactly the headings below. Any other first row fails the run with nothing written.

| Column | Heading | Owner | What the sync does |
| --- | --- | --- | --- |
| A | Job ID | Sync | Writes a source job ID on a new row and keeps the ID already there. |
| B to H | Company, Role, Location, Fit, Why it fits, Watch out for, Link | Sync | Overwrites a cell that differs. |
| I | Status | You | Restores a status it refused, or a blank or unknown one. |
| J, K | Recruiter, Note | You | Writes them once, on a new row, and never again. |
| L | Updated | Sync | Overwrites a cell that differs. |
| M | Sync note | Sync | Writes the facts and refusals below. |
| N | Pay | Sync | Overwrites a cell that differs. Pay is a number, so keep this column's number format free of a currency sign. A currency format fails the read-back. |
| O, P | Funding, Funding source | Sync | Overwrites a cell that differs. |

Column A holds a source job ID, not a `real_job_id`, because source IDs survive when Jobfeed merges two postings into one job.

### How edits flow

Each run reads the tab and saves your edits to Jobfeed, so an edited job stays on the sheet and held whatever its funding check finds. It then makes any funding lookups that are due. If it made any, it reads the tab and saves edits again, because lookups can take minutes and you may have sorted or edited in that time. It then reads the recruiter sheet again and writes one atomic batch. A run with nothing to change makes no write. After a write it reads the tab again and compares the columns it owns, row by row.

A row counts as edited when its Status, Recruiter, or Note differs from what Jobfeed stored. The status labels are `to ask`, `asked`, `with recruiter`, `passed`, and `apply myself`. A blank status keeps the stored status, and a recruiter or note on that row still saves.

The sheet wins over the API while the sync runs. An edit sent to the API is overwritten or refused on the next run, because the old value in the cell then counts as your edit.

Updates never move a row, so your sorting and filtering stay. New rows go at the end, and then the sync sorts A2:P by Fit, highest first. Sorting moves whole rows, so your columns stay with their job.

A row whose job Jobfeed no longer returns is deleted if its Status is `to ask` and its Recruiter and Note are blank. Otherwise you wrote something, so the sync keeps it and says in Sync note that the edit was not saved. A second row for the same job follows the same rule, and the first row stays the row. Blank rows are skipped. A row with an unreadable Job ID is never deleted, and it gets a Sync note.

### What Sync note says

The note has up to two parts, joined by ` · `.

- **Facts.** Each run recomputes `Posting closed`, `Applied`, `No longer qualifies`, or `Funding check pending`.
- **Refusal.** `Refused: <message>` appears when Jobfeed rejects an edit. It stays until a later run saves an edit for that row.

### How the sync reaches the sheet

This machine has no Google credentials, so each Sheets call is one `claude -p` run that uses the claude.ai Google Sheets connector on the Haiku model. A hook replaces the tool input with the planned payload and allows the call one time, so the model never handles the payload and posting text in the sheet cannot change what is sent. The sync reads the result only from the raw tool result, never from the model's text.

Each call costs about half a cent. A run that changes nothing makes one call, and a run that writes makes three. A run with a lookup makes one more read. The model's context holds the whole tab on a read, so a read costs more as the tab grows. Funding lookups are separate runs with no access to the sheet. The sync's log line gives their count, found, errors, and cost, and it holds no posting text.

### When a run fails

A failed run gives one line with no posting text in it. The dashboard logs it as `recruiter_sheet_sync_failed` with that line as the reason, and the command writes it on stderr and exits nonzero. The next run starts from scratch, and nothing is retried within a run. To see the logs, run `journalctl --user -u jobfeed-dashboard.service -g 'recruiter.sheet' -n 50`.

If some lookups fail, the run logs each one and succeeds. If at least 2 lookups ran and all of them failed, it finishes the sheet work and then fails. A single failed lookup only logs, and the next run retries it.

| What you see | Cause | What to do |
| --- | --- | --- |
| `unexpected header` | The first row of the tab changed. No lookup ran. | Restore the headings. |
| `all N funding lookups failed` | At least 2 lookups ran and all of them failed. The sheet work finished. | Read the logged reasons with the log command below. Run `claude` by hand if the reason is `claude exited`. |
| `claude was not found on PATH` | The dashboard's `PATH` lacks `claude`. | Fix `PATH` in `jobfeed-dashboard.service`. |
| `claude exited`, `timed out`, or `no tool result` | Claude Code or the connector failed, or its sign-in expired. | Run `claude` by hand and check the connector. |
| `the Sheets tool returned an error` | Google refused the batch, and the batch writes nothing. | Read the rest of the line. |
| `exceeds grid limits` in that error | The tab has run out of rows. | Add rows at the bottom of the tab. |
| `does not hold what was written` | The tab differs from the batch after the write. | Check the named job IDs. The next run repairs the columns the sync owns. |

Each deleted row is replaced by a new blank row below the data, which inherits the Status dropdown from the row above, so the tab keeps its size. The one exception is a run that removes every job, because a new row would inherit the header's styling. A row you add by hand needs the Status dropdown copied to it.

Three things a run cannot prevent:

- If you sort the tab or insert a row between the run's read and its write, the batch uses stale row positions. It can then write to the wrong row or delete the wrong row. The read-back fails the run if a column the sync owns came out wrong. That gap is the length of one `claude` call.
- If a run saves an edit and then fails, a `Refused:` note for that row stays until the next saved edit.
- If you edit a row while a run's lookups are in progress and a lookup disqualifies its job, the edit is not saved, because the job has already left the list. The row stays, and Sync note says the edit was not saved. The job is not held, so autopilot may apply to it. An edit made before the run starts is saved first, so this cannot happen to it.

## Deploy the funding check

The new code needs two things, in this order:

1. Add the headings `Pay`, `Funding`, and `Funding source` to N1, O1, and P1 of the Jobs tab. Until then, every run fails with `unexpected header`.
2. Restart the dashboard. It creates the `company_funding` table when it starts. Until then, the sync fails and the autopilot worker admits nothing, because the sheet cannot be read.

Older code refuses a database with a table it does not know. To roll back, stop the services that use the database and run this from the combined root before you start the older code:

```sh
.venv/bin/python -c "import sqlite3; sqlite3.connect('jobfeed/data/jobfeed.sqlite').execute('DROP TABLE company_funding')"
```

This deletes the funding records, and the next run after you return to the new code looks them up again.

## Remove the hourly timer

An earlier version synced from an hourly systemd timer. The dashboard now does it, so remove the old units and restart the dashboard:

```sh
systemctl --user disable --now jobfeed-recruiter-sheet-sync.timer
rm ~/.config/systemd/user/jobfeed-recruiter-sheet-sync.service ~/.config/systemd/user/jobfeed-recruiter-sheet-sync.timer
systemctl --user daemon-reload
systemctl --user restart jobfeed-dashboard.service
```

## The Discord alert

After each sync, the dashboard runs `python -m pipeline.recruiter_sheet_alert --root resume-application-pipeline --result success`, or `--result failure --reason` with the failure line. It finds `resume-application-pipeline` beside `jobfeed`, or at `JOBFEED_APPLY_ROOT` when that is set. A failed alert is logged as `recruiter_sheet_alert_failed` and never changes the evaluation. The alert keeps its count in `resume-application-pipeline/.pipeline/recruiter-sheet-sync-alert.json`.

- One failure says nothing, because the next evaluation syncs again.
- The second failure in a row posts one message to the bot's main channel. No more messages follow while the sync stays failed.
- The message says the Google Sheet may be stale and your edits may not be reaching the pipeline. A run whose only failure is lookups did finish its sheet work. It also gives the reason and the log command. Autopilot's hold still protects jobs on the sheet, because the hold reads Jobfeed and not the Google Sheet.
- A successful sync resets the count. If an alert went out, it posts one recovery message.
- If a post fails, the alert stays owed and the next sync tries again.
- An empty `google_sheet_id` syncs nothing and tells the alert nothing.
- A sync you run by hand tells the alert nothing.
- A sync that a dashboard shutdown cut off tells the alert nothing.
- Missing or corrupt state counts as healthy with no failures.

The reason is the sync's failure line, cut to 300 characters. The alert uses the bot token in `~/.config/jobfeed-applier/discord.env`, and it mentions nobody. The Discord intake and the Applier bridge both skip bot messages, so an alert is never read as your input.
