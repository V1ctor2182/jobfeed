"""The courier and its hook, with the real hook script run by a fake ``claude``."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jobfeed.adapters import sheet_courier
from jobfeed.adapters.sheet_courier import SheetCourier, SheetCourierError
from tests.support.fake_claude import install_fake_claude

BLOCKING_EXIT = 2
PRIVATE = 0o700
READ_TOOL = "mcp__claude_ai_Google_Sheets__get_values"
WRITE_TOOL = "mcp__claude_ai_Google_Sheets__update_spreadsheet"
HOOK = Path(sheet_courier.__file__).with_name("sheet_courier_hook.py")
BATCH = [
    {
        "deleteDimension": {
            "range": {"dimension": "ROWS", "startIndex": 1, "endIndex": 2, "sheetId": 0}
        }
    }
]


def run_hook(plan: Path, tool: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(HOOK), str(plan)],
        input=json.dumps({"tool_name": tool, "tool_input": {"spreadsheetId": "x"}}),
        capture_output=True,
        text=True,
        check=False,
    )


def answer(done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.json"
    planned = {"spreadsheetId": "real", "requests": BATCH}
    path.write_text(json.dumps({"tool": WRITE_TOOL, "input": planned}))
    return path


def test_hook_allows_tool_search_any_number_of_times(plan: Path) -> None:
    for _ in range(2):
        assert answer(run_hook(plan, "ToolSearch")) == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    assert not plan.with_name("called").exists()


def test_hook_replaces_the_input_of_the_planned_tool_once(plan: Path) -> None:
    first = answer(run_hook(plan, WRITE_TOOL))
    assert first["permissionDecision"] == "allow"
    assert first["updatedInput"] == {"spreadsheetId": "real", "requests": BATCH}
    second = answer(run_hook(plan, WRITE_TOOL))
    assert second["permissionDecision"] == "deny"
    assert "updatedInput" not in second


@pytest.mark.parametrize("tool", ["Bash", "Read", READ_TOOL, "mcp__other__tool", ""])
def test_hook_denies_every_unplanned_tool_without_using_the_call(
    plan: Path, tool: str
) -> None:
    assert answer(run_hook(plan, tool))["permissionDecision"] == "deny"
    assert not plan.with_name("called").exists()


def test_hook_blocks_when_it_cannot_read_the_plan(tmp_path: Path) -> None:
    done = run_hook(tmp_path / "missing.json", WRITE_TOOL)
    assert done.returncode == BLOCKING_EXIT
    assert done.stdout == ""


def test_read_takes_the_values_from_the_raw_result_not_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"], ["12"]])
    courier = SheetCourier("sheet-1")
    assert courier.read() == [["Job ID"], ["12"]]
    assert courier.cost_usd == pytest.approx(0.005)
    [call] = fake.calls()
    assert call["tool"] == READ_TOOL
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert "--no-session-persistence" in argv
    workdir = Path(call["cwd"])
    assert not workdir.exists()


def test_a_result_as_a_list_of_text_blocks_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    fake.behave(READ_TOOL, "list_content")
    assert SheetCourier("sheet-1").read() == [["Job ID"]]


def test_the_workdir_is_private_and_is_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[tuple[int, Path]] = []
    real_run = subprocess.run

    def spying_run(*args: Any, **kwargs: Any) -> Any:
        cwd = kwargs["cwd"]
        modes.append((stat.S_IMODE(Path(cwd).stat().st_mode), Path(cwd)))
        assert kwargs["stdin"] == subprocess.DEVNULL
        return real_run(*args, **kwargs)

    install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    monkeypatch.setattr(sheet_courier.subprocess, "run", spying_run)
    SheetCourier("sheet-1").read()
    assert modes[0][0] == PRIVATE


def test_a_write_sends_the_plan_not_the_models_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"], ["12"]])
    SheetCourier("sheet-1").write(BATCH)
    assert fake.writes() == [{"spreadsheetId": "sheet-1", "requests": BATCH}]


def test_a_second_write_attempt_is_denied_and_the_batch_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"], ["12"], ["13"]])
    fake.behave(WRITE_TOOL, "call_twice")
    SheetCourier("sheet-1").write(BATCH)
    assert len(fake.writes()) == 1
    assert fake.values() == [["Job ID"], ["13"]]


def test_a_call_before_the_tool_loaded_does_not_use_up_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"], ["12"]])
    fake.behave(WRITE_TOOL, "unloaded_first")
    SheetCourier("sheet-1").write(BATCH)
    assert len(fake.writes()) == 1


@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ("tool_error", "returned an error"),
        ("no_tool_result", "no tool result"),
        ("nonzero", "exited 3: claude broke"),
    ],
)
def test_a_failed_call_raises_with_a_clear_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str, reason: str
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    fake.behave(READ_TOOL, behavior)
    with pytest.raises(SheetCourierError, match=reason):
        SheetCourier("sheet-1").read()


def test_a_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    fake.behave(READ_TOOL, "hang")
    monkeypatch.setattr(sheet_courier, "_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(SheetCourierError, match="timed out"):
        SheetCourier("sheet-1").read()


def test_claude_missing_from_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SheetCourierError, match="not found on PATH"):
        SheetCourier("sheet-1").read()


def test_a_read_that_is_not_json_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    fake.behave(READ_TOOL, "not_json")
    with pytest.raises(SheetCourierError, match="not valid JSON"):
        SheetCourier("sheet-1").read()


def test_a_tool_error_never_repeats_the_cell_text_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_claude(tmp_path, monkeypatch, [["Job ID"]])
    fake.behave(WRITE_TOOL, "tool_error")
    secret = 'Posting "text"\nwith a newline'
    batch = [
        {
            "updateCells": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": 1,
                    "endRowIndex": 2,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "rows": [{"values": [{"userEnteredValue": {"stringValue": secret}}]}],
                "fields": "userEnteredValue",
            }
        }
    ]
    with pytest.raises(SheetCourierError) as caught:
        SheetCourier("sheet-1").write(batch)
    message = str(caught.value)
    assert "returned an error" in message
    assert "Posting" not in message
    assert "\n" not in message
