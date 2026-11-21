"""Scheduled foreground work may preempt backfill without retrying paid posts."""

import importlib

import pytest

from scripts import jobfeed_scheduled_cycle as scheduler
from scripts.jobfeed_scheduled_cycle import run_cycle


class API:
    def __init__(self, active, statuses=None):
        self.active = iter(active)
        self.statuses = statuses or {"scan-1": "succeeded", "evaluate-1": "succeeded"}
        self.requests = []
        self.waits = []

    def request(self, path, payload=None):
        self.requests.append((path, payload))
        if path == "/api/runs/active":
            return {"runs": next(self.active)}
        if path == "/api/sources/jobright/status":
            return {"connected": True}
        return {"run_id": path.rsplit("/", 1)[-1] + "-1"}

    def wait(self, run_id):
        self.waits.append(run_id)
        return {"run_id": run_id, "status": self.statuses[run_id]}


@pytest.mark.parametrize(
    "active",
    [
        [[{"source": "application-backfill"}], []],
        [[], [{"source": "application-backfill"}]],
        [
            [{"source": "application-backfill"}],
            [{"source": "application-backfill"}],
        ],
    ],
)
def test_backfill_at_scan_or_evaluation_check_does_not_skip(active):
    api = API(active)
    assert run_cycle(api.request, api.wait) == "succeeded"
    assert [item for item in api.requests if item[1] is not None] == [
        ("/api/runs/scan", {"source": "all"}),
        (
            "/api/runs/evaluate",
            {"stage": "both", "scope": "backlog", "corpus": "unrated"},
        ),
    ]
    assert api.waits == ["scan-1", "evaluate-1"]


@pytest.mark.parametrize("source", ["all", "evaluate", "", None])
def test_other_active_work_still_blocks_initial_scan(source):
    api = API([[{"source": "application-backfill"}, {"source": source}]])
    assert run_cycle(api.request, api.wait) == "skipped_active"
    assert api.requests == [("/api/runs/active", None)]
    assert api.waits == []


def test_missing_source_active_work_still_blocks_initial_scan():
    api = API([[{"run_id": "unknown"}]])
    assert run_cycle(api.request, api.wait) == "skipped_active"
    assert api.waits == []


def test_other_work_starting_after_scan_blocks_evaluation():
    api = API([[], [{"source": "application-backfill"}, {"source": "evaluate"}]])
    assert run_cycle(api.request, api.wait) == "skipped_active"
    assert api.waits == ["scan-1"]
    assert [item for item in api.requests if item[1] is not None] == [
        ("/api/runs/scan", {"source": "all"})
    ]


@pytest.mark.parametrize("status", ["failed", "stopped", "partial", "cancelled"])
def test_unsuccessful_scan_never_starts_evaluation(status):
    api = API([[]], {"scan-1": status})
    with pytest.raises(RuntimeError, match=f"scan scan-1 {status}; cycle stopped"):
        run_cycle(api.request, api.wait)
    assert api.waits == ["scan-1"]
    assert [item for item in api.requests if item[1] is not None] == [
        ("/api/runs/scan", {"source": "all"})
    ]


def test_a_scan_with_one_failed_source_still_evaluates_what_the_others_stored():
    api = API([[], []])

    def wait(run_id):
        if run_id == "scan-1":
            return {
                "status": "failed",
                "failed_stage": "source",
                "failed_source": "speedyapply",
            }
        return api.wait(run_id)

    assert run_cycle(api.request, wait) == "evaluated_after_source_failure"
    assert api.requests[-1] == (
        "/api/runs/evaluate",
        {"stage": "both", "scope": "backlog", "corpus": "unrated"},
    )


def test_a_source_failure_in_evaluation_still_stops_the_cycle():
    api = API([[], []], {"scan-1": "succeeded", "evaluate-1": "failed"})
    original = api.wait
    api.wait = lambda run_id: dict(original(run_id), failed_stage="source")
    with pytest.raises(RuntimeError, match="evaluate evaluate-1 failed; cycle stopped"):
        run_cycle(api.request, api.wait)


def test_ambiguous_evaluation_post_is_not_retried():
    api = API([[], []])

    def request(path, payload=None):
        response = api.request(path, payload)
        if path == "/api/runs/evaluate":
            raise TimeoutError("response missing after evaluation accepted")
        return response

    with pytest.raises(TimeoutError, match="response missing"):
        run_cycle(request, api.wait)
    assert api.waits == ["scan-1"]
    assert [item for item in api.requests if item[0] == "/api/runs/evaluate"] == [
        (
            "/api/runs/evaluate",
            {"stage": "both", "scope": "backlog", "corpus": "unrated"},
        )
    ]


def test_wait_observes_only_the_exact_created_run(monkeypatch):
    calls, sleeps = [], []
    responses = iter([{"status": "running"}, {"status": "succeeded"}])
    clock = iter([0, 0, 10])

    def request(path):
        calls.append(path)
        return next(responses)

    monkeypatch.setattr(scheduler, "request_json", request)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scheduler.time, "sleep", sleeps.append)
    assert scheduler.wait_run("created-run-id") == {"status": "succeeded"}
    assert calls == ["/api/runs/created-run-id", "/api/runs/created-run-id"]
    assert sleeps == [10]


def test_observation_timeout_does_not_stop_or_retry_server_work(monkeypatch):
    calls = []
    clock = iter([0, 7201])
    monkeypatch.setattr(scheduler, "request_json", calls.append)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="two-hour observation window"):
        scheduler.wait_run("created-run-id")
    assert calls == []


def test_terminal_database_lock_scan_is_resumed_once_before_evaluation():
    api = API([[], [], []])
    original_wait = api.wait

    def wait(run_id):
        if run_id == "scan-1":
            return {"status": "failed", "failure_message": "database is locked"}
        if run_id == "retry-1":
            return {"status": "succeeded"}
        return original_wait(run_id)

    assert run_cycle(api.request, wait) == "succeeded"
    assert ("/api/runs/scan-1/retry", {}) in api.requests
    assert api.requests[-1][0] == "/api/runs/evaluate"


def test_resumed_busy_scan_failure_stops_without_second_retry_or_evaluation():
    api = API([[], []])

    def wait(_run_id):
        return {"status": "failed", "failure_message": "database is locked"}

    with pytest.raises(RuntimeError, match="cycle stopped"):
        run_cycle(api.request, wait)
    posts = [path for path, payload in api.requests if payload is not None]
    assert posts == ["/api/runs/scan", "/api/runs/scan-1/retry"]


def test_busy_scan_retry_post_timeout_is_not_repeated():
    api = API([[], []])

    def request(path, payload=None):
        if path.endswith("/retry"):
            api.requests.append((path, payload))
            raise TimeoutError("unknown POST result")
        return api.request(path, payload)

    def wait(_run_id):
        return {"status": "failed", "failure_message": "database is locked"}

    with pytest.raises(TimeoutError):
        run_cycle(request, wait)
    assert len([p for p, _ in api.requests if p.endswith("/retry")]) == 1


def test_a_closed_chrome_does_not_stop_the_other_sources():
    api = API([[], []])

    def request(path, payload=None):
        if path == "/api/sources/jobright/status":
            return {"connected": False}
        return api.request(path, payload)

    assert run_cycle(request, api.wait) == "succeeded"
    assert ("/api/sources/jobright/status", None) not in api.requests
    assert api.waits == ["scan-1", "evaluate-1"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[sources.jobright]\nenabled = false\n", False),
        ("[sources.jobright]\nenabled = true\n", True),
        ("[sources.ats]\nenabled = true\n", False),
    ],
)
def test_jobright_enabled_reads_the_config(tmp_path, text, expected):
    config = tmp_path / "config.toml"
    config.write_text(text)
    assert scheduler.jobright_enabled(config) is expected


def test_requests_go_to_the_dashboard_port_unless_overridden(monkeypatch):
    monkeypatch.delenv("JOBFEED_API_URL", raising=False)
    assert importlib.reload(scheduler).BASE == "http://127.0.0.1:8765"
    monkeypatch.setenv("JOBFEED_API_URL", "http://127.0.0.1:9000/")
    assert importlib.reload(scheduler).BASE == "http://127.0.0.1:9000"
    monkeypatch.delenv("JOBFEED_API_URL")
    importlib.reload(scheduler)


def test_cycle_before_setup_finishes_skips_without_calling_the_dashboard(
    tmp_path, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(scheduler, "request_json", calls.append)
    monkeypatch.setattr(scheduler, "run_cycle", calls.append)
    monkeypatch.setattr(
        "sys.argv", ["cycle", "--config", str(tmp_path / "config.toml")]
    )
    assert scheduler.main() == 0
    assert calls == []
    assert "setup is not finished" in capsys.readouterr().out
