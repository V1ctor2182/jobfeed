"""PreToolUse hook for one courier call: the planned tool runs once, with the plan.

The input the model sends is replaced, so posting text cannot steer the call. Claude
Code runs a failing hook's tool anyway unless the hook exits 2, so every failure here
exits 2. This file runs alone under ``python -I`` and imports only the standard library.
"""

import json
import os
import sys
from pathlib import Path


def _answer(decision: str, **fields: object) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            **fields,
        }
    }


def _first_call(marker: Path) -> bool:
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _decide(event: dict[str, object], plan_path: Path) -> dict[str, object]:
    # Replaying a batch of row deletes would destroy rows, so the marker is exclusive.
    plan = json.loads(plan_path.read_text())
    tool = event.get("tool_name")
    if tool == "ToolSearch":
        return _answer("allow")
    if tool != plan["tool"]:
        return _answer("deny", permissionDecisionReason="This tool is not planned.")
    if not _first_call(plan_path.with_name("called")):
        return _answer("deny", permissionDecisionReason="The call already ran.")
    return _answer("allow", updatedInput=plan["input"])


def main() -> int:
    """Read the event from stdin and print the answer.

    Returns:
        0 with an answer printed, or 2, which blocks the tool call.
    """
    try:
        print(json.dumps(_decide(json.load(sys.stdin), Path(sys.argv[1]))))
    except Exception as exc:
        print(f"sheet courier hook failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
