# Pipeline coordination reference

Its private `.pipeline/coordination.sqlite3` records job ownership, browser observations, shared workflow claims, waiting requests, and account identities. It does not add tables to Jobfeed or replace the worker. The Claude Applier records its tab through `register_tab`; the `pipeline.cli resources` commands below remain for the Codex Rerun and restart path that is still to be removed.

## Job tabs and recovery

New task material contains `resource-contract.json`. Official create acknowledgment binds the original chat to its job. Existing chats receive an idempotent contract and binding before their next queued continuation. A pending legacy continuation can migrate before dispatch. Unknown or sent messages are not rewritten or resent.

The `resources` command accepts one JSON object on stdin for mutations. Browser observations use this shape:

```json
{
  "expected_revision": 0,
  "tabs": [
    {
      "browser_id": "iab",
      "tab_id": "observed-tab-id",
      "opener_tab_id": null,
      "url": "https://example.invalid/apply",
      "state": "observed"
    }
  ]
}
```

`observe --job-id JOB --owner ORIGINAL_CHAT` requires the current revision. Supported states are `observed`, `preserved`, `missing`, `needs_reconciliation`, and `closed`. Missing and closed observations require an `observation_id`. Only verified closure releases the durable tab association. Missing tabs remain reserved. Popups require an opener from the same job. URL storage retains the origin, excluding applicant paths and query strings.

The registry records observations. It does not claim a tab through the harness. Recovery still requires official tools to confirm the exact browser, tab, and current ownership. A matching URL is insufficient.

After Jobfeed removes a submitted queue row, cleanup can retain the original owner only when its durable closeout and execution binding both match. Cleanup owners can observe tabs and release or reconcile existing claims. They cannot acquire new resources or restart an application.

## Shared account identities

Only the active scope controller can configure an account for jobs already in that scope:

```sh
python3 -m pipeline.cli resources --scope /absolute/path/batch.json \
  --controller EXISTING_CONTROLLER_ID configure-account <<'JSON'
{
  "profile_id": "shared-iab",
  "tenant_origin": "https://example.invalid",
  "account_alias": "confirmed-applicant",
  "job_ids": [123, 456]
}
JSON
```

Profile IDs and account aliases are confirmed opaque identifiers, not emails, passwords, or OTPs. The controller must use one account alias for the same real account. Tenant input accepts an origin, excluding credentials, application paths, and query strings.

The server derives the account conflict key from tenant and account alias. The same account conflicts across browser profiles. Profile-specific references remain separate for observation and grants. Models select an existing registered key from `snapshot.accounts`. An unregistered account key fails before any claim write. Missing or ambiguous identities wait for controller configuration.

## Shared workflow claims

| Method | Required stdin fields | Result |
| --- | --- | --- |
| `acquire --job-id JOB --owner CHAT` | `kind`, `resource_key`, `request_id`, `owner_token` | `held` or a durable `waiting` request |
| `release` | `claim_id`, `token`, `evidence` | Records a known outcome and releases the claim |
| `uncertain` | `claim_id`, `token`, `evidence` | Preserves exclusive ownership for reconciliation |
| `reconcile --controller CHAT` | `claim_id`, `expected_revision`, `evidence` | Releases only after authorized idle verification |

Kinds are `account_mutation`, `native_auth`, `native_workflow`, `word`, and `clipboard_foreground`. Account mutation and native authentication share one conflict namespace for the registered account. Native workflow claims use the app bundle ID as their global key. A native account flow acquires account first, then app. It releases app before account and does not nest another account while holding the app.

The caller creates and retains a random token before acquiring. Snapshots omit tokens and token hashes. Reusing a request with different ownership or identity is rejected. Exact completion replay returns the saved result. Conflicting completion evidence is rejected.

Known-outcome evidence contains `status` and an opaque `observation_id`. Status is `completed`, `not_started`, or `cancelled`. Uncertain evidence contains a reason code. An owner cannot turn an uncertain claim into a normal release.

Reconciliation requires the current controller, current revision, and a workspace-relative JSON idle observation. The artifact identifies scope, job, original owner, claim, observation ID, `resource_state: idle`, and a timezone-aware observation time after the uncertain state. It also requires either the original owner's token acknowledgment or a matching, correctly timed recorded turn stop. A stopped turn alone does not prove that all desktop activity ended. The controller must verify current idle state. These are cooperative controller checks, not per-tool interception.

Claims never expire automatically. Capacity release, elapsed time, and missing PIDs do not authorize takeover.
