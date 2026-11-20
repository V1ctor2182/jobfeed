"""Judge an Applier's saved Submit receipt with Jobfeed's own evidence check, against what that Applier was given.

2026-11-18: two jobs had a receipt file that Jobfeed rejects for showing no acknowledgement (an Oracle "Are You Still
With Us?" dialog, and the posting page itself). Requeue counted the file as proof of a submission, so neither job could
move. A receipt Jobfeed accepts settles its job as submitted, and one rejected for want of an acknowledgement proves
nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

RECEIPT = 'submission-receipt.json'
# The page capture saved beside the receipt. It is evidence only through the receipt.
RECEIPT_FILES = frozenset({RECEIPT, 'confirmation-page.json'})
NO_ACKNOWLEDGEMENT = 'application_acknowledgement_missing'
ACCEPTED, UNACKNOWLEDGED = 'accepted', 'unacknowledged'


def applier_context(launch):
    """(evidence folder, posting URL, company) the launch's Applier was given, or None when its config cannot be read."""
    try:
        args = json.loads((Path(json.loads(launch['config'])['cwd']) / 'applier-mcp.json').read_text())['mcpServers']['applier']['args']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    value = lambda flag: args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None  # noqa: E731
    folder, url = value('--evidence-dir'), value('--job-url')
    return (Path(folder), url, value('--company')) if folder and url else None


def verdict(job_id, launch):
    """ACCEPTED when Jobfeed accepts the receipt, UNACKNOWLEDGED when it rejects it only for showing no acknowledgement,
    otherwise None: no receipt, no readable context, or any other rejection, which stays for the existing rules."""
    from jobfeed.submission_evidence import EvidenceValidationError, read_confirmation
    context = applier_context(launch)
    if context is None or not (context[0] / RECEIPT).is_file():
        return None
    folder, url, company = context
    try:
        read_confirmation(folder, job_id, url, company=company)
    except EvidenceValidationError as exc:
        return UNACKNOWLEDGED if str(exc) == NO_ACKNOWLEDGEMENT else None
    except (OSError, ValueError):
        return None
    return ACCEPTED


def settle(store):
    """Record as submitted each attempt whose Applier is not running and whose saved receipt Jobfeed accepts. Its Jobfeed
    sync follows through the outbox, as when an Applier records it itself."""
    events = []
    attempts = store._read("SELECT s.job_id,j.owner FROM applier_submits s JOIN applier_jobs j ON j.job_id=s.job_id "
                           "WHERE s.state IN ('requested','unknown')", ())
    for row in attempts:
        launch = store._read('SELECT * FROM applier_launches WHERE launch_id=?', (row['owner'],))
        if not launch or launch[0]['state'] == 'running' or verdict(row['job_id'], launch[0]) != ACCEPTED:
            continue
        try:
            store.record_submit_outcome(row['job_id'], row['owner'], 'submitted', 'The worker found a saved receipt that Jobfeed accepts.')
        except ValueError:
            continue
        events.append({'event': 'receipt_settled_submitted', 'job_id': row['job_id']})
    return events
