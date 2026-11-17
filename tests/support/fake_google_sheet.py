"""A fake Google Sheet, and a fake ``claude`` executable that reaches it.

``Grid`` applies batch update requests the way the Sheets API does. Run as a script,
this file stands in for ``claude -p``: it reads the hook from ``--settings``, runs it on
each tool call as Claude Code does, and calls the fake sheet with the hook's input. A
run with ``--json-schema`` is a funding lookup, which it answers as scripted, by default
with a recent round that its search results back up.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

COLUMNS = 16
FIT = 4
TAB_ROWS = 1000
BLOCKING_EXIT = 2


class GridError(ValueError):
    """The Sheets API would reject this request and the whole batch with it."""


def _cell_text(value: dict[str, Any]) -> str:
    entered = value.get("userEnteredValue", {})
    if "numberValue" in entered:
        return str(int(entered["numberValue"]))
    return entered.get("stringValue", "")


def _update_cells(cells: list[list[str]], body: dict[str, Any]) -> None:
    """Write a block of cells. Time complexity: linear in the cells written."""
    if body["fields"] != "userEnteredValue":
        raise GridError(f"unexpected fields mask {body['fields']}")
    area = body["range"]
    for row_offset, row in enumerate(body["rows"]):
        row_index = area["startRowIndex"] + row_offset
        if row_index >= area["endRowIndex"] or row_index >= len(cells):
            raise GridError(f"row {row_index} exceeds grid limits")
        for col_offset, value in enumerate(row["values"]):
            cells[row_index][area["startColumnIndex"] + col_offset] = _cell_text(value)


def _delete_dimension(cells: list[list[str]], body: dict[str, Any]) -> None:
    area = body["range"]
    if area["dimension"] != "ROWS" or area["endIndex"] > len(cells):
        raise GridError("bad delete range")
    del cells[area["startIndex"] : area["endIndex"]]


def _insert_dimension(cells: list[list[str]], body: dict[str, Any]) -> None:
    area = body["range"]
    start, end = area["startIndex"], area["endIndex"]
    if area["dimension"] != "ROWS" or start > len(cells):
        raise GridError("bad insert range")
    if body["inheritFromBefore"] is not True or start <= 1:
        raise GridError("a new row must inherit from a row below the header")
    cells[start:start] = [[""] * COLUMNS for _ in range(end - start)]


def _fit_key(row: list[str]) -> tuple[int, float]:
    return (1, 0.0) if not row[FIT] else (0, -float(row[FIT]))


def _sort_range(cells: list[list[str]], body: dict[str, Any]) -> None:
    area = body["range"]
    if area["endRowIndex"] > len(cells):
        raise GridError("sort range exceeds grid limits")
    if body["sortSpecs"] != [{"dimensionIndex": FIT, "sortOrder": "DESCENDING"}]:
        raise GridError("unexpected sort")
    start, end = area["startRowIndex"], area["endRowIndex"]
    cells[start:end] = sorted(cells[start:end], key=_fit_key)


_REQUESTS: dict[str, Callable[[list[list[str]], dict[str, Any]], None]] = {
    "updateCells": _update_cells,
    "deleteDimension": _delete_dimension,
    "insertDimension": _insert_dimension,
    "sortRange": _sort_range,
}


class Grid:
    """One tab: a fixed number of rows that a delete makes smaller."""

    def __init__(self, values: Sequence[Sequence[str]], rows: int = TAB_ROWS) -> None:
        self.cells = [
            [*row, *[""] * (COLUMNS - len(row))] for row in (list(v) for v in values)
        ]
        self.cells += [[""] * COLUMNS for _ in range(rows - len(self.cells))]

    def apply(self, requests: Sequence[dict[str, Any]]) -> None:
        """Apply a batch, all of it or none of it, in order.

        Args:
            requests: The ``batchUpdate`` requests.
        """
        cells = [list(row) for row in self.cells]
        for request in requests:
            [(kind, body)] = request.items()
            _REQUESTS[kind](cells, body)
        self.cells = cells

    def values(self) -> list[list[str]]:
        """Read as ``get_values`` does, trimming trailing blanks and blank rows.

        Time complexity: linear in the cells of the tab.

        Returns:
            The rows from the top of the tab.
        """
        rows: list[list[str]] = []
        for row in self.cells:
            trimmed = list(row)
            while trimmed and not trimmed[-1]:
                trimmed.pop()
            rows.append(trimmed)
        while rows and not rows[-1]:
            rows.pop()
        return rows


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def _assistant_calls(tool: str, tool_id: str, tool_input: dict[str, Any]) -> None:
    block = {"type": "tool_use", "id": tool_id, "name": tool, "input": tool_input}
    _emit(
        {"type": "assistant", "message": {"role": "assistant", "content": [block]}},
    )


def _tool_result(tool_id: str, content: Any, *, is_error: bool = False) -> None:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    _emit({"type": "user", "message": {"role": "user", "content": [block]}})


def _run_hook(command: str, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    done = subprocess.run(
        command,
        shell=True,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode == BLOCKING_EXIT:
        return {"permissionDecision": "deny", "permissionDecisionReason": done.stderr}
    return json.loads(done.stdout)["hookSpecificOutput"]


class _Fake:
    """One ``claude -p`` run against the sheet stored in a directory."""

    def __init__(self, directory: Path, tool: str, hook: str) -> None:
        self.directory, self.tool, self.hook = directory, tool, hook
        self.next_id = 0

    def _new_id(self) -> str:
        self.next_id += 1
        return f"toolu_{self.next_id}"

    def _sheet(self) -> Grid:
        state = json.loads((self.directory / "sheet.json").read_text())
        return Grid(state["values"], state["rows"])

    def _save(self, grid: Grid) -> None:
        state = {"values": grid.values(), "rows": len(grid.cells)}
        (self.directory / "sheet.json").write_text(json.dumps(state))

    def _execute(self, tool_input: dict[str, Any]) -> str:
        grid = self._sheet()
        if "range" in tool_input:
            values = grid.values()
            return json.dumps({"range": tool_input["range"], "values": values})
        grid.apply(tool_input["requests"])
        self._save(grid)
        with (self.directory / "writes.jsonl").open("a") as out:
            out.write(json.dumps(tool_input) + "\n")
        return json.dumps({"spreadsheetId": tool_input["spreadsheetId"], "replies": []})

    def search(self) -> None:
        tool_id = self._new_id()
        _assistant_calls("ToolSearch", tool_id, {"query": f"select:{self.tool}"})
        if _run_hook(self.hook, "ToolSearch", {})["permissionDecision"] != "allow":
            raise SystemExit("ToolSearch denied")
        _tool_result(tool_id, [{"type": "tool_reference", "tool_name": self.tool}])

    def call(self, behavior: str = "ok") -> None:
        tool_id = self._new_id()
        dummy = {"spreadsheetId": "x", "range": "A1"}
        _assistant_calls(self.tool, tool_id, dummy)
        decision = _run_hook(self.hook, self.tool, dummy)
        if decision["permissionDecision"] != "allow":
            reason = decision.get("permissionDecisionReason", "denied")
            _tool_result(tool_id, reason, is_error=True)
            return
        tool_input = decision.get("updatedInput", dummy)
        if behavior == "tool_error":
            _tool_result(tool_id, _error_quoting(tool_input), is_error=True)
            return
        if behavior == "not_json":
            _tool_result(tool_id, "something else entirely")
            return
        try:
            text = self._execute(tool_input)
        except GridError as exc:
            _tool_result(tool_id, str(exc), is_error=True)
            return
        as_list = behavior == "list_content"
        _tool_result(tool_id, [{"type": "text", "text": text}] if as_list else text)

    def tamper(self) -> None:
        grid = self._sheet()
        grid.cells[1][1] = "Tampered"
        self._save(grid)


def _behave(fake: _Fake, behavior: str) -> int:
    if behavior == "hang":
        time.sleep(30)
        return 0
    if behavior == "unloaded_first":
        tool_id = fake._new_id()
        _assistant_calls(fake.tool, tool_id, {})
        _tool_result(tool_id, "No such tool available", is_error=True)
    fake.search()
    if behavior != "no_tool_result":
        fake.call(behavior)
    if behavior == "call_twice":
        fake.call()
    if behavior == "tamper":
        fake.tamper()
    _emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": '{"values":[["model text"]]}'}],
            },
        }
    )
    _emit({"type": "result", "subtype": "success", "total_cost_usd": 0.005})
    if behavior == "nonzero":
        print("claude broke", file=sys.stderr)
        return 3
    return 0


def _funding_answer(company: str, script: dict[str, Any]) -> dict[str, Any] | None:
    slug = re.sub(r"[^a-z0-9]+", "-", company.casefold()).strip("-")
    recent = datetime.now(UTC).date() - timedelta(days=30)
    answer = {
        "found": True,
        "round": "Series A",
        "amount_usd": 17000000,
        "announced_on": recent.isoformat(),
        "source_url": f"https://news.example/{slug}",
        "raised_by": company,
    }
    if "answer" not in script:
        return answer
    return None if script["answer"] is None else {**answer, **script["answer"]}


def _funding_events(company: str, script: dict[str, Any]) -> None:
    if script.get("init", True):
        _emit(
            {
                "type": "system",
                "subtype": "init",
                "tools": script.get("tools", ["StructuredOutput", "WebSearch"]),
                "mcp_servers": script.get("mcp_servers", []),
            }
        )
    answer = _funding_answer(company, script)
    url = (answer or {}).get("source_url")
    _assistant_calls("WebSearch", "toolu_1", {"query": company})
    _tool_result(
        "toolu_1", script.get("search_text", f'Links: [{{"url": "{url}"}}] Raised.')
    )
    if "other_tool_text" in script:
        _assistant_calls("OtherTool", "toolu_2", {})
        _tool_result("toolu_2", script["other_tool_text"])
    result = {"type": "result", "subtype": "success", "total_cost_usd": 0.025}
    if answer is not None:
        result["structured_output"] = answer
    _emit(result)


def _funding_run(directory: Path, argv: Sequence[str]) -> int:
    """Act as ``claude -p`` for one funding lookup, scripted by company name."""
    prompt = argv[argv.index("-p") + 1]
    match = re.search(r'Company: ("(?:[^"\\]|\\.)*")\.', prompt)
    company = json.loads(match.group(1)) if match else ""
    with (directory / "funding_calls.jsonl").open("a") as out:
        call = {
            "company": company,
            "prompt": prompt,
            "argv": list(argv),
            "cwd": os.getcwd(),
            "stdin_is_null": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
        }
        out.write(json.dumps(call) + "\n")
    scripts = json.loads((directory / "funding.json").read_text())
    script = scripts.get(company, scripts.get("*", {}))
    if script.get("hang"):
        time.sleep(30)
    if "tab_during" in script:
        state = json.loads((directory / "sheet.json").read_text())
        state["values"] = script["tab_during"]
        (directory / "sheet.json").write_text(json.dumps(state))
    _funding_events(company, script)
    return int(script.get("exit", 0))


def _error_quoting(tool_input: dict[str, Any]) -> str:
    texts = [
        cell["userEnteredValue"]["stringValue"]
        for request in tool_input.get("requests", [])
        for row in request.get("updateCells", {}).get("rows", [])
        for cell in row["values"]
        if "stringValue" in cell.get("userEnteredValue", {})
    ]
    return f"Invalid request near {json.dumps(texts)} and {' '.join(texts)}"


def main() -> int:
    """Act as ``claude -p`` for one tool call.

    Returns:
        The exit code.
    """
    directory = Path(os.environ["FAKE_SHEET_DIR"])
    if "--json-schema" in sys.argv:
        return _funding_run(directory, sys.argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", dest="prompt", required=True)
    parser.add_argument("--allowedTools", required=True)
    parser.add_argument("--settings", required=True)
    args, _ = parser.parse_known_args()
    settings = json.loads(Path(args.settings).read_text())
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    with (directory / "calls.jsonl").open("a") as out:
        out.write(
            json.dumps(
                {"tool": args.allowedTools, "cwd": os.getcwd(), "argv": sys.argv[1:]}
            )
            + "\n"
        )
    behaviors_path = directory / "behaviors.json"
    behaviors = json.loads(behaviors_path.read_text())
    behavior = behaviors.get(args.allowedTools, ["ok"])
    current = behavior.pop(0) if behavior else "ok"
    behaviors_path.write_text(json.dumps(behaviors))
    return _behave(_Fake(directory, args.allowedTools, hook), current)


if __name__ == "__main__":
    raise SystemExit(main())
