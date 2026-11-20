"""`jobfeed doctor` reports what works and what to fix, without changing anything."""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfeed.cli import cli
from jobfeed.cli.doctor import Check, Host, run_checks
from jobfeed.onboarding_types import ProviderOnboardingState
from jobfeed.remote_access import RemoteAccessStore
from jobfeed.web.app import create_web_app

onboard_module = importlib.import_module("jobfeed.cli.onboard")
doctor_module = importlib.import_module("jobfeed.cli.doctor")
GB = 1024**3
METRICS = "http://127.0.0.1:7655"
LINK = "https://quiet-river.trycloudflare.com"
SERVICE_PATH = "/home/u/.local/bin:/usr/bin:/bin"


class ConnectedClaude:
    """A signed-in Claude subscription with both models chosen."""

    def state(self) -> ProviderOnboardingState:
        return ProviderOnboardingState(
            provider="claude_cli",
            connected=True,
            quick_model="haiku",
            detailed_model="sonnet",
        )


class FakeHost(Host):
    """A Linux server whose commands, disk and network the test controls."""

    platform = "linux"

    def __init__(self) -> None:
        self.outputs: dict[tuple[str, ...], tuple[int, str, str] | None] = {
            ("systemctl", "--user", "is-enabled", "jobfeed-dashboard.service"): (
                0,
                "enabled\n",
                "",
            ),
            ("systemctl", "--user", "is-active", "jobfeed-dashboard.service"): (
                0,
                "active\n",
                "",
            ),
            ("loginctl",): (0, "yes\n", ""),
            ("systemctl", "--user", "show", "jobfeed-dashboard.service"): (
                0,
                f"PATH={SERVICE_PATH}\n",
                "",
            ),
            ("systemctl", "--user", "is-enabled", "jobfeed-daily-scan.timer"): (
                1,
                "disabled\n",
                "",
            ),
            ("git", "-C"): (128, "", "fatal: not a git repository"),
            ("claude",): (0, "OK\n", ""),
        }
        self.calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
        self.free = 20 * GB
        self.pages: dict[str, int] = {}

    def run(
        self,
        *command: str,
        timeout: float = 15,  # noqa: ARG002
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        self.calls.append((command, env))
        for size in range(len(command), 0, -1):
            if command[:size] in self.outputs:
                output = self.outputs[command[:size]]
                if output is None:
                    return None
                code, out, err = output
                return subprocess.CompletedProcess(command, code, out, err)
        return subprocess.CompletedProcess(command, 1, "", "")

    def free_bytes(self, path: Path) -> int:  # noqa: ARG002
        return self.free

    def get(self, url: str) -> httpx.Response | None:
        if url == f"{METRICS}/quicktunnel":
            hostname = LINK.removeprefix("https://")
            if "tunnel" in self.pages:
                return httpx.Response(200, json={"hostname": hostname})
            return None
        code = self.pages.get(url)
        return None if code is None else httpx.Response(code)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    web_app = create_web_app()
    with TestClient(web_app):
        yield web_app


def _connect(app: FastAPI) -> Any:
    @contextmanager
    def connect(url: str) -> Iterator[TestClient]:
        yield TestClient(app, base_url=url, headers={"Origin": url})

    return connect


def _rows(
    app: FastAPI | None, host: FakeHost, root: Path, **kwargs: Any
) -> dict[str, Check]:
    @contextmanager
    def down(url: str) -> Iterator[httpx.Client]:
        transport = httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        )
        with httpx.Client(base_url=url, transport=transport) as client:
            yield client

    rows = run_checks(
        host=host,
        root=root,
        url="http://127.0.0.1:7654",
        skip_ai=kwargs.get("skip_ai", False),
        metrics_url=METRICS,
        connect=_connect(app) if app is not None else down,
    )
    return {row.check: row for row in rows}


def test_a_fresh_install_says_setup_is_not_finished(
    app: FastAPI, tmp_path: Path
) -> None:
    rows = _rows(app, FakeHost(), tmp_path)
    assert rows["Dashboard"].status == "ok"
    assert rows["Background service"].status == "ok"
    assert rows["Setup finished"].status == "fail"
    assert "onboard status" in rows["Setup finished"].fix
    assert rows["Last scan"].status == "off"
    assert rows["Daily scan"].status == "off"
    assert rows["Private link"].status == "off"
    assert rows["Disk"].status == "ok"


def test_a_stopped_dashboard_still_reports_the_machine(tmp_path: Path) -> None:
    host = FakeHost()
    host.outputs[("systemctl", "--user", "is-active", "jobfeed-dashboard.service")] = (
        3,
        "failed\n",
        "",
    )
    rows = _rows(None, host, tmp_path)
    assert rows["Dashboard"].status == "fail"
    assert rows["Background service"].status == "fail"
    assert "restart" in rows["Background service"].fix
    assert "Setup finished" not in rows
    assert rows["Disk"].status == "ok"


def test_the_ai_answers_from_the_service_path(app: FastAPI, tmp_path: Path) -> None:
    app.state.onboarding_provider_service = ConnectedClaude()
    host = FakeHost()
    rows = _rows(app, host, tmp_path)
    assert rows["AI answers"].status == "ok"
    command, env = next(call for call in host.calls if call[0][0] == "claude")
    assert command[-2:] == ("--model", "haiku")
    assert env is not None
    assert env["PATH"] == SERVICE_PATH

    host.outputs[("claude",)] = (1, "", "Invalid API key · Please run /login\n")
    failed = _rows(app, host, tmp_path)["AI answers"]
    assert failed.status == "fail"
    assert "claude auth login" in failed.fix

    host.outputs[("claude",)] = None
    assert "cannot find" in _rows(app, host, tmp_path)["AI answers"].detail
    assert _rows(app, host, tmp_path, skip_ai=True)["AI answers"].status == "off"


def test_a_failed_daily_scan_is_reported(app: FastAPI, tmp_path: Path) -> None:
    host = FakeHost()
    host.outputs[("systemctl", "--user", "is-enabled", "jobfeed-daily-scan.timer")] = (
        0,
        "enabled\n",
        "",
    )
    host.outputs[("systemctl", "--user", "show", "jobfeed-daily-scan.timer")] = (
        0,
        "Sat 2026-11-21 13:00:00 UTC\n",
        "",
    )
    host.outputs[("systemctl", "--user", "show", "jobfeed-daily-scan.service")] = (
        0,
        "success\n",
        "",
    )
    ok = _rows(app, host, tmp_path)["Daily scan"]
    assert ok.status == "ok"
    assert "Sat 2026-11-21 13:00:00 UTC" in ok.detail

    host.outputs[("systemctl", "--user", "show", "jobfeed-daily-scan.service")] = (
        0,
        "exit-code\n",
        "",
    )
    assert _rows(app, host, tmp_path)["Daily scan"].status == "fail"


def test_the_private_link_must_ask_for_the_code(app: FastAPI, tmp_path: Path) -> None:
    host = FakeHost()
    RemoteAccessStore(tmp_path / "data" / "remote-access.json").create()
    assert _rows(app, host, tmp_path)["Private link"].status == "warn"

    host.pages["tunnel"] = 1
    host.pages[f"{LINK}/api/health"] = 401
    assert _rows(app, host, tmp_path)["Private link"].status == "ok"

    host.pages[f"{LINK}/api/health"] = 200
    exposed = _rows(app, host, tmp_path)["Private link"]
    assert exposed.status == "fail"
    assert "disable --now" in exposed.fix


def test_a_full_disk_fails(app: FastAPI, tmp_path: Path) -> None:
    host = FakeHost()
    host.free = GB // 2
    assert _rows(app, host, tmp_path)["Disk"].status == "fail"
    host.free = GB + GB // 2
    assert _rows(app, host, tmp_path)["Disk"].status == "warn"


def test_the_command_exits_1_and_says_how_to_fix(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        onboard_module, "_client_factory", partial(_TestClientFactory, app)
    )
    monkeypatch.setattr(doctor_module, "Host", FakeHost)
    result = CliRunner().invoke(cli, ["doctor", "--no-ai"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "✅ Dashboard: running, database ok" in result.output
    assert "❌ Setup finished" in result.output
    assert "→ Run `jobfeed onboard status`" in result.output
    assert "checks failed." in result.output


class _TestClientFactory(TestClient):
    def __init__(self, app: FastAPI, *, trust_env: bool, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__(app, **kwargs)

    def __enter__(self) -> _TestClientFactory:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_runs_report_failures_but_not_restarts() -> None:
    runs: list[dict[str, Any]] = [
        {
            "source": "all",
            "status": "failed",
            "failure_code": "interrupted",
            "started_at": "2026-11-20T08:06:00+00:00",
        },
        {
            "source": "evaluate",
            "status": "failed",
            "failure_code": "runtime_error",
            "failure_message": "Claude CLI is not signed in",
            "started_at": "2026-11-20T08:10:00+00:00",
        },
    ]

    class Runs:
        def get(self, path: str) -> dict[str, Any]:
            assert path.startswith("/api/runs")
            return {"runs": runs}

    rows = {
        row.check: row
        for row in doctor_module.check_runs(cast(Any, Runs()), daily=True)
    }
    assert rows["Last scan"].status == "warn"
    assert rows["Last evaluation"].status == "fail"
    assert "not signed in" in rows["Last evaluation"].detail


class FakeDashboard:
    """Answers the two reads the sign-in check makes."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs

    def get(self, path: str) -> dict[str, Any]:
        if path == "/api/config":
            return {
                "configured": True,
                "sources": {
                    "handshake": {"enabled": True},
                    "linkedin_extension": {"enabled": True},
                    "jobright": {"enabled": False},
                },
            }
        return {"runs": self.runs}


def _scan(**progress: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "all",
        "status": "failed",
        "started_at": "2026-11-18T09:10:00+00:00",
        "scan_progress": progress,
        "scan_stats": {"linkedin-extension": {"discovered": 41}},
    }


@pytest.mark.parametrize(
    ("message", "detail"),
    [
        ("HTTP 401", "signed out of Handshake"),
        ("No authenticated job request observed. Sign in and retry.", "signed out"),
        ("Jobright Chrome extension is not connected", "not connected"),
        ("Discovery returned no result", "returned no jobs"),
    ],
)
def test_each_signed_in_site_reports_its_last_scan(message: str, detail: str) -> None:
    runs = [
        _scan(
            handshake={"phase": "failed", "message": message},
            **{"linkedin-extension": {"phase": "completed"}},
        )
    ]
    rows = {
        row.check: row
        for row in doctor_module.check_signed_in_sites(cast(Any, FakeDashboard(runs)))
    }
    assert rows["Handshake sign-in"].status == "fail"
    assert detail in rows["Handshake sign-in"].detail
    assert rows["LinkedIn sign-in"].status == "ok"
    assert "41 jobs found" in rows["LinkedIn sign-in"].detail
    assert "Jobright sign-in" not in rows
    if "signed out" in detail:
        assert "sign me in to Handshake again" in rows["Handshake sign-in"].fix


def test_a_site_not_scanned_yet_is_not_a_failure() -> None:
    rows = doctor_module.check_signed_in_sites(cast(Any, FakeDashboard([])))
    assert {row.status for row in rows} == {"off"}
