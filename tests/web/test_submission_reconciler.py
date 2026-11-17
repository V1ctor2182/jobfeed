import asyncio
import os
import signal
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest

from jobfeed.application_submissions import ApplicationSubmissionService
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import fake_context

RUN_RECONCILER = ApplicationSubmissionService.run_reconciler


@pytest.fixture(autouse=True)
def real_reconciler(_isolated_application_reconciler, monkeypatch, tmp_path):
    monkeypatch.setattr(ApplicationSubmissionService, "run_reconciler", RUN_RECONCILER)
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(tmp_path / "runtime"))


def service(tmp_path, *, rows=("preparing",)):
    root = tmp_path / "runtime"
    ledger = root / ".pipeline/dashboard-submissions.sqlite3"
    ledger.parent.mkdir(parents=True)
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE requests(state TEXT, prompt TEXT)")
        db.executemany(
            "INSERT INTO requests VALUES(?,NULL)", [(state,) for state in rows]
        )
    return ApplicationSubmissionService(root, tmp_path / "jobfeed.sqlite3")


def script_process(monkeypatch, script):
    spawn = asyncio.create_subprocess_exec
    processes = []
    calls = []
    created = asyncio.Event()

    async def launch(*args, **kwargs):
        calls.append((args, kwargs))
        process = await spawn(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return processes, calls, created


async def test_absent_or_empty_ledger_starts_no_process_and_creates_no_runtime(
    tmp_path, monkeypatch
):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    absent = ApplicationSubmissionService(tmp_path / "absent", tmp_path / "jobs.db")
    await absent.reconcile_pending()
    assert not absent.root.exists()
    await service(tmp_path, rows=()).reconcile_pending()
    spawn.assert_not_called()


async def test_terminal_unsent_rows_do_not_spawn_reconciliation(tmp_path, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await service(
        tmp_path, rows=("submitted", "blocked", "turn_ended")
    ).reconcile_pending()
    spawn.assert_not_called()


async def test_reconciliation_sends_only_empty_payload_to_existing_pipeline(
    tmp_path, monkeypatch
):
    target = service(tmp_path)
    processes, calls, _ = script_process(
        monkeypatch,
        'import json,sys; assert json.load(sys.stdin)=={}; '
        'print(json.dumps({"checked":1,"results":[],"deferred":False}))',
    )
    await target.reconcile_pending()
    assert calls[0][0] == (
        str(target.root / ".venv/bin/python"),
        "-m",
        "pipeline.dashboard_submission",
        "--root",
        str(target.root),
        "--jobfeed-db",
        str(target.jobfeed_db),
        "reconcile-pending",
    )
    assert calls[0][1]["start_new_session"] is True
    assert processes[0].returncode == 0


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_shutdown_or_timeout_kills_and_reaps_its_process_group(
    tmp_path, monkeypatch, outcome
):
    target = service(tmp_path)
    processes, _, created = script_process(monkeypatch, "import time; time.sleep(60)")
    killed = []
    kill = os.killpg

    def record_kill(pid, sig):
        killed.append((pid, sig))
        kill(pid, sig)

    monkeypatch.setattr(os, "killpg", record_kill)
    task = asyncio.create_task(
        target.reconcile_pending(timeout=0.02 if outcome == "timeout" else 60)
    )
    await asyncio.wait_for(created.wait(), 2)
    if outcome == "cancel":
        task.cancel()
    with pytest.raises(
        TimeoutError if outcome == "timeout" else asyncio.CancelledError
    ):
        await task
    assert killed == [(processes[0].pid, signal.SIGKILL)]
    assert processes[0].returncode is not None


@pytest.mark.parametrize(
    "script",
    ['print("not-json")', 'import sys; print("{}"); sys.exit(1)', 'print("x"*2100000)'],
)
async def test_invalid_failed_or_oversized_response_is_not_success(
    tmp_path, monkeypatch, script
):
    target = service(tmp_path)
    processes, _, _ = script_process(monkeypatch, script)
    with pytest.raises(ValueError):
        await target.reconcile_pending()
    assert processes[0].returncode is not None


async def test_timeout_kills_children_even_after_cli_has_exited(tmp_path, monkeypatch):
    target = service(tmp_path)
    processes, _, _ = script_process(
        monkeypatch,
        'import subprocess,sys; '
        'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
        'sys.exit(0)',
    )
    killed = []
    kill = os.killpg

    def record_kill(pid, sig):
        killed.append((pid, sig))
        kill(pid, sig)

    monkeypatch.setattr(os, "killpg", record_kill)
    with pytest.raises(TimeoutError):
        await target.reconcile_pending(timeout=0.1)
    assert processes[0].returncode == 0
    assert killed == [(processes[0].pid, signal.SIGKILL)]


async def test_cancellation_during_process_creation_still_reaps_child(
    tmp_path, monkeypatch
):
    target = service(tmp_path)
    spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def delayed_spawn(*_args, **kwargs):
        process = await spawn(
            sys.executable, "-c", "import time; time.sleep(60)", **kwargs
        )
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(target.reconcile_pending())
    await asyncio.wait_for(created.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_loop_recovers_errors_without_overlapping_passes_or_http(
    tmp_path, monkeypatch
):
    target = service(tmp_path)
    calls = []
    active = False
    ready = asyncio.Event()

    async def reconcile():
        nonlocal active
        assert not active
        active = True
        calls.append("tick")
        await asyncio.sleep(0)
        active = False
        if len(calls) == 1:
            raise OSError("temporary unavailable")
        ready.set()

    monkeypatch.setattr(target, "reconcile_pending", reconcile)
    task = asyncio.create_task(target.run_reconciler(interval=0.001))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["tick", "tick"]


async def test_app_lifespan_starts_recovers_and_stops_without_http(
    tmp_path, monkeypatch
):
    app = build_web_app(fake_context(), static_dir=tmp_path / "no-dist")
    calls = []
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def reconcile():
        calls.append("startup")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(
        app.state.application_submissions, "reconcile_pending", reconcile
    )
    for _ in range(2):
        started.clear()
        stopped.clear()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 1)
            assert not stopped.is_set()
        assert stopped.is_set()
    assert calls == ["startup", "startup"]
