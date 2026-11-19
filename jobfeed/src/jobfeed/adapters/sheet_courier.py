"""Reach the Google Sheet through ``claude -p`` and the claude.ai Sheets connector.

This machine has no Google credentials, so a model runs each call as a courier. A
hook supplies the real input and only the raw tool result is read, so the payload never
depends on the model and posting text in the sheet cannot corrupt or steer a call.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from jobfeed.adapters.claude_stream import tool_results

_TOOL_PREFIX = "mcp__claude_ai_Google_Sheets__"
_READ_TOOL = f"{_TOOL_PREFIX}get_values"
_WRITE_TOOL = f"{_TOOL_PREFIX}update_spreadsheet"
_READ_RANGE = "Jobs!A:P"
_PROMPT_ARGUMENTS = {
    _READ_TOOL: '{"spreadsheetId":"x","range":"A1"}',
    _WRITE_TOOL: '{"spreadsheetId":"x","requests":[]}',
}
_HOOK = Path(__file__).with_name("sheet_courier_hook.py")
_TIMEOUT_SECONDS = 170
_MAX_BUDGET_USD = "0.25"
# Claude Code limits a connector result to 25000 tokens by default, and a full tab can
# exceed that. A cut-off result is not JSON, so it would fail the run.
_MAX_RESULT_TOKENS = "500000"
_DETAIL_LENGTH = 200


class SheetCourierError(RuntimeError):
    """A call failed. Its message is one line and holds no text the sync sent."""


def _sent_texts(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "stringValue":
                yield item
            else:
                yield from _sent_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _sent_texts(item)


def _detail(text: str, tool_input: dict[str, Any]) -> str:
    """Make an error line safe to log: a tool may quote the cell text it was sent."""
    for sent in sorted(set(_sent_texts(tool_input)), key=len, reverse=True):
        text = text.replace(sent, "[cell text]")
        text = text.replace(json.dumps(sent)[1:-1], "[cell text]")
    return " ".join(text.split())[:_DETAIL_LENGTH]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return ""


def _reply_text(results: Sequence[dict[str, Any]], tool_input: dict[str, Any]) -> str:
    """Pick the call that ran. The hook lets one through, so at most one succeeds.

    Any other result is an error: a call made before the deferred tool loaded, or a
    repeat that the hook denied.
    """
    for result in results:
        if not result.get("is_error"):
            return _text(result.get("content"))
    if not results:
        raise SheetCourierError("claude finished with no tool result")
    detail = _detail(_text(results[0].get("content")), tool_input)
    raise SheetCourierError(f"the Sheets tool returned an error: {detail}")


class SheetCourier:
    """One spreadsheet's reads and writes, each a separate ``claude -p`` run."""

    def __init__(self, spreadsheet_id: str) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.cost_usd = 0.0

    def read(self) -> list[list[str]]:
        """Read the Jobs tab.

        Returns:
            The rows from the top of the tab, with trailing blanks trimmed.

        Raises:
            SheetCourierError: If the call fails or the result has the wrong shape.
        """
        text = self._call(
            _READ_TOOL, {"spreadsheetId": self.spreadsheet_id, "range": _READ_RANGE}
        )
        try:
            values = json.loads(text).get("values", [])
            return [[str(cell) for cell in row] for row in values]
        except (ValueError, AttributeError, TypeError) as exc:
            raise SheetCourierError("the Sheets read was not valid JSON") from exc

    def write(self, requests: Sequence[dict[str, Any]]) -> None:
        """Send one atomic batch of requests.

        Args:
            requests: The ``batchUpdate`` requests.

        Raises:
            SheetCourierError: If the call fails.
        """
        self._call(
            _WRITE_TOOL,
            {"spreadsheetId": self.spreadsheet_id, "requests": list(requests)},
        )

    def _call(self, tool: str, tool_input: dict[str, Any]) -> str:
        with tempfile.TemporaryDirectory(prefix="recruiter-sheet-") as directory:
            workdir = Path(directory)
            plan = workdir / "plan.json"
            plan.write_text(json.dumps({"tool": tool, "input": tool_input}))
            settings = workdir / "settings.json"
            settings.write_text(json.dumps(_settings(plan)))
            done = self._run_claude(tool, workdir, settings)
        results, cost = tool_results(done.stdout, tool)
        self.cost_usd += cost
        if done.returncode != 0:
            detail = _detail(done.stderr, tool_input)
            raise SheetCourierError(f"claude exited {done.returncode}: {detail}")
        return _reply_text(results, tool_input)

    def _run_claude(
        self, tool: str, workdir: Path, settings: Path
    ) -> subprocess.CompletedProcess[str]:
        prompt = (
            f"Call the Google Sheets {tool.removeprefix(_TOOL_PREFIX)} tool exactly "
            f"once with arguments {_PROMPT_ARGUMENTS[tool]}. "
            "Then reply with just the word done."
        )
        command = [
            "claude",
            "-p",
            prompt,
            "--model",
            "haiku",
            "--allowedTools",
            tool,
            "--settings",
            str(settings),
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            _MAX_BUDGET_USD,
        ]
        try:
            return subprocess.run(
                command,
                cwd=workdir,
                env={**os.environ, "MAX_MCP_OUTPUT_TOKENS": _MAX_RESULT_TOKENS},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SheetCourierError("claude was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SheetCourierError(
                f"claude timed out after {_TIMEOUT_SECONDS} seconds"
            ) from exc


def _settings(plan: Path) -> dict[str, Any]:
    command = shlex.join([sys.executable, "-I", str(_HOOK.resolve()), str(plan)])
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": ".*", "hooks": [{"type": "command", "command": command}]}
            ]
        }
    }
