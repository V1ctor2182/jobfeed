#!/usr/bin/env python3
"""One scan of every enabled source, then unrated evaluation, \
through the running dashboard's API."""

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import time
import tomllib
import urllib.request
from pathlib import Path

# The Jobfeed dashboard serves the API on 127.0.0.1:8765.
BASE = os.environ.get("JOBFEED_API_URL", "http://127.0.0.1:8765").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("jobfeed.schedule")


def request_json(path, payload=None):
    """Call loopback once; never retry an ambiguous POST and duplicate paid work."""
    request = urllib.request.Request(
        BASE + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def wait_run(run_id):
    """Observe the exact run; timeout leaves existing server work untouched."""
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        run = request_json(f"/api/runs/{run_id}")
        if run["status"] != "running":
            return run
        time.sleep(10)
    raise TimeoutError(f"Run {run_id} exceeded two-hour observation window")


def jobright_enabled(config=ROOT / "config.toml"):
    """Whether the Jobright source, which needs the Chrome extension, is enabled."""
    with Path(config).open("rb") as stream:
        sources = tomllib.load(stream).get("sources", {})
    return sources.get("jobright", {}).get("enabled") is True


def run_cycle(request=request_json, wait=wait_run, *, jobright=False):
    """Skip foreground work; API preempts backfill before scan or evaluation."""
    if _has_foreground_run(request("/api/runs/active")["runs"]):
        LOG.info("Skipped: another run is active")
        return "skipped_active"
    if jobright and not request("/api/sources/jobright/status")["connected"]:
        raise RuntimeError("Chrome extension is disconnected; scan not started")
    outcome = "succeeded"
    for phase, payload in (
        ("scan", {"source": "all"}),
        ("evaluate", {"stage": "both", "scope": "backlog", "corpus": "unrated"}),
    ):
        if phase == "evaluate" and _has_foreground_run(
            request("/api/runs/active")["runs"]
        ):
            LOG.info("Skipped evaluation: another run became active")
            return "skipped_active"
        run_id = request(f"/api/runs/{phase}", payload)["run_id"]
        LOG.info("Started %s run=%s", phase, run_id)
        result = wait(run_id)
        LOG.info(
            "Finished %s run=%s status=%s errors=%s cost=%s",
            phase,
            run_id,
            result["status"],
            result.get("errors"),
            result.get("total_llm_cost_usd"),
        )
        if (
            phase == "scan"
            and result["status"] == "failed"
            and result.get("failure_message") == "database is locked"
        ):
            if _has_foreground_run(request("/api/runs/active")["runs"]):
                return "skipped_active"
            # A confirmed terminal attempt is safe to resume through its journal.
            # Never retry an ambiguous POST, a paid evaluation, or a second failure.
            run_id = request(f"/api/runs/{run_id}/retry", {})["run_id"]
            LOG.info("Resumed database-busy scan run=%s", run_id)
            result = wait(run_id)
            LOG.info("Finished resumed scan run=%s status=%s", run_id, result["status"])
        if _source_failure(phase, result):
            # The other sources' jobs are stored, and one source's outage must not
            # leave them unrated.
            LOG.warning(
                "Scan run=%s failed in source %s; "
                "evaluating what the other sources stored",
                run_id,
                result.get("failed_source"),
            )
            outcome = "evaluated_after_source_failure"
        elif result["status"] != "succeeded":
            raise RuntimeError(f"{phase} {run_id} {result['status']}; cycle stopped")
    return outcome


def _source_failure(phase, result):
    """A scan that finished with only some sources' work failed."""
    return (
        phase == "scan"
        and result["status"] == "failed"
        and result.get("failed_stage") == "source"
    )


def _has_foreground_run(runs):
    """Only recognized application backfill yields to scheduled foreground work."""
    return any(run.get("source") != "application-backfill" for run in runs)


def main():
    """Configure bounded logs and an OS-released lock for cron, launchd or systemd."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Read-only connectivity check"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    args = parser.parse_args()
    if not args.config.is_file():
        # Onboarding writes config.toml when it finishes. A timer that fires
        # earlier, such as a missed run caught up after the server wakes,
        # must not scan with no profile or searches.
        print(f"Skipped: Jobfeed setup is not finished ({args.config} is missing)")
        return 0
    jobright = jobright_enabled(args.config)
    if args.check:
        status = {"api": BASE, "active": request_json("/api/runs/active")}
        if jobright:
            status["bridge"] = request_json("/api/sources/jobright/status")
        print(json.dumps(status))
        return 0
    directory = ROOT / "data" / "scheduler"
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        directory / "cycle.log",
        maxBytes=2_000_000,
        backupCount=5,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    with (directory / "cycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Skipped: scheduled cycle already running")
            return 0
        try:
            LOG.info("Cycle started")
            LOG.info("Cycle result=%s", run_cycle(jobright=jobright))
        except Exception:
            LOG.exception("Cycle failed; no automatic POST retry")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
