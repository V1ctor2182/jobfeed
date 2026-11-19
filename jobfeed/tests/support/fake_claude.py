"""Install the fake ``claude`` executable on PATH for one test."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.support import fake_google_sheet
from tests.support.fake_google_sheet import TAB_ROWS


@dataclass
class FakeClaude:
    """The state a fake ``claude`` run reads and leaves in a directory."""

    directory: Path

    def behave(self, tool: str, *behaviors: str) -> None:
        """Script the next runs of a tool.

        Args:
            tool: The tool whose runs to script.
            behaviors: One behavior per run.
        """
        path = self.directory / "behaviors.json"
        scripted = json.loads(path.read_text())
        scripted[tool] = list(behaviors)
        path.write_text(json.dumps(scripted))

    def funding(self, company: str, **script: Any) -> None:
        """Script every funding lookup of one company.

        Args:
            company: The company's name as the prompt quotes it, or * for any company.
            script: How the run behaves, with any of ``answer`` (fields over the default
                answer, or None for no structured output), ``search_text``,
                ``other_tool_text``, ``tools``, ``mcp_servers``, ``init``, ``exit``,
                ``hang``, and ``tab_during`` (the rows the user leaves on the tab while
                the lookup runs).
        """
        path = self.directory / "funding.json"
        scripted = json.loads(path.read_text())
        scripted[company] = script
        path.write_text(json.dumps(scripted))

    def funding_calls(self) -> list[dict[str, Any]]:
        """Read the log of funding lookups.

        Returns:
            Every lookup so far.
        """
        return self._lines("funding_calls.jsonl")

    def _lines(self, name: str) -> list[dict[str, Any]]:
        path = self.directory / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def calls(self) -> list[dict[str, Any]]:
        """Read the log of ``claude`` runs.

        Returns:
            Every run so far.
        """
        return self._lines("calls.jsonl")

    def writes(self) -> list[dict[str, Any]]:
        """Read the log of writes.

        Returns:
            Every write input that reached the sheet.
        """
        return self._lines("writes.jsonl")

    def values(self) -> list[list[str]]:
        """Read the tab.

        Returns:
            The rows as ``get_values`` reads them.
        """
        return json.loads((self.directory / "sheet.json").read_text())["values"]

    def row_count(self) -> int:
        """Read the size of the tab.

        Returns:
            How many rows the tab has, blank ones included.
        """
        return json.loads((self.directory / "sheet.json").read_text())["rows"]

    def set_values(self, values: Sequence[Sequence[str]]) -> None:
        """Change the tab as the user would between runs.

        Args:
            values: The rows the tab now holds.
        """
        path = self.directory / "sheet.json"
        state = json.loads(path.read_text())
        path.write_text(json.dumps({**state, "values": list(values)}))


def install_fake_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: Sequence[Sequence[str]],
    rows: int = TAB_ROWS,
) -> FakeClaude:
    """Put a fake ``claude`` first on PATH.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Sets PATH for this test.
        values: The rows the tab holds.
        rows: The size of the tab.

    Returns:
        The handle for scripting and reading the fake.
    """
    directory = tmp_path / "fake-claude"
    directory.mkdir()
    (directory / "sheet.json").write_text(
        json.dumps({"values": list(values), "rows": rows})
    )
    (directory / "behaviors.json").write_text("{}")
    (directory / "funding.json").write_text("{}")
    script = directory / "claude"
    command = shlex.join([sys.executable, fake_google_sheet.__file__])
    script.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SHEET_DIR", str(directory))
    return FakeClaude(directory)
