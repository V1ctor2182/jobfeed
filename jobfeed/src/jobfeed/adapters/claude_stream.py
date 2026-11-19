"""Read the events of a ``claude -p --output-format stream-json`` run."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


def stream_events(stdout: str) -> Iterator[dict[str, Any]]:
    """Parse a run's output, skipping any line that is not a JSON object.

    Args:
        stdout: The run's standard output.

    Returns:
        An iterator over the events in order.
    """
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _blocks(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ()
    return (block for block in content if isinstance(block, dict))


def tool_results(stdout: str, tool: str) -> tuple[list[dict[str, Any]], float]:
    """Find the raw results of one tool and the run's cost.

    Other tools' results arrive on the same event type, so a result counts only when its
    tool_use_id belongs to a call of this tool. Time complexity: linear in the size of
    the stream.

    Args:
        stdout: The run's standard output.
        tool: The tool's name.

    Returns:
        The tool's result blocks in order, and the run's cost in dollars.
    """
    call_ids: set[str] = set()
    results: list[dict[str, Any]] = []
    cost = 0.0
    for event in stream_events(stdout):
        if event.get("type") == "result":
            cost = float(event.get("total_cost_usd") or 0)
        for block in _blocks(event):
            if block.get("type") == "tool_use" and block.get("name") == tool:
                call_ids.add(block.get("id", ""))
            elif (
                block.get("type") == "tool_result"
                and block.get("tool_use_id") in call_ids
            ):
                results.append(block)
    return results, cost
