"""Small stdlib client for the Codex App Server JSON-RPC protocol."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class AppServerError(RuntimeError):
    pass


class AppServerTimeout(AppServerError):
    pass


class AppServerTurnFailed(AppServerError):
    """A persisted turn failed, rather than merely having no final answer yet."""
    pass


class CodexAppServer:
    """Own one App Server process and create durable Codex threads through it."""

    def __init__(
        self,
        command: tuple[str, ...] = ("codex", "-c", 'web_search="disabled"', "app-server", "--stdio"),
        *,
        approve_requests: bool = False,
    ):
        self.command = command
        self.approve_requests = approve_requests
        self._next_id = 1
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._pending: list[dict[str, Any]] = []
        self._stderr: list[str] = []
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "CodexAppServer":
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "resume-pipeline", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")
        return self

    def __exit__(self, *_: object) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                self._stderr.append(f"invalid App Server output: {line.rstrip()}")

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppServerError("App Server is not running")
        self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        response = self._wait_for(lambda item: item.get("id") == request_id, timeout)
        if "error" in response:
            raise AppServerError(f"{method} failed: {response['error']}")
        return response["result"]

    def _wait_for(self, predicate: Any, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self._pending):
                if predicate(message):
                    return self._pending.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self._stderr[-1] if self._stderr else "no server error output"
                raise AppServerTimeout(f"timed out waiting for App Server ({detail})")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                detail = self._stderr[-1] if self._stderr else "no server error output"
                raise AppServerTimeout(f"timed out waiting for App Server ({detail})") from exc
            if "id" in message and "method" in message:
                response = self._approval_response(message)
                self._send(response)
                continue
            if predicate(message):
                return message
            self._pending.append(message)

    def _approval_response(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self.approve_requests:
            return {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": f"unsupported client request: {message['method']}",
                },
            }
        method = message["method"]
        if method == "item/commandExecution/requestApproval":
            return {"id": message["id"], "result": {"decision": "accept"}}
        if method == "item/permissions/requestApproval":
            permissions = message.get("params", {}).get("permissions", {})
            return {
                "id": message["id"],
                "result": {"permissions": permissions, "scope": "turn"},
            }
        return {
            "id": message["id"],
            "error": {"code": -32601, "message": f"unsupported client request: {method}"},
        }

    def start_thread(
        self,
        cwd: Path,
        name: str,
        *,
        sandbox: str = "read-only",
        approval_policy: str = "never",
        ephemeral: bool = False,
        allow_hidden_persistent: bool = False,
    ) -> str:
        # 2026-10-16: persistent threads created here never show in Codex Desktop,
        # so an 原 Agent must be created there (desktop_delivery). Only the legacy
        # advisor may still create hidden persistent threads, and says so.
        if not ephemeral and not allow_hidden_persistent:
            raise AppServerError("hidden persistent thread refused: create application sessions in Codex Desktop (desktop_delivery)")
        result = self.request(
            "thread/start",
            {
                "cwd": str(cwd.resolve()),
                "runtimeWorkspaceRoots": [str(cwd.resolve())],
                "ephemeral": ephemeral,
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "threadSource": "resume-pipeline",
            },
        )
        thread_id = result["thread"]["id"]
        if not ephemeral:
            self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return thread_id

    def resume_thread(
        self,
        thread_id: str,
        cwd: Path,
        *,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
    ) -> None:
        self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(cwd.resolve()),
                "runtimeWorkspaceRoots": [str(cwd.resolve())],
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "excludeTurns": True,
            },
        )

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        timeout: float = 180,
        *,
        model: str | None = None,
        effort: str | None = None,
        output_schema: dict[str, Any] | None = None,
        on_turn_started: Any = None,
        cancel_event: threading.Event | None = None,
        execution_gate: Any = None,
        execution_action: str = "apply",
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        if output_schema is not None:
            params["outputSchema"] = output_schema
        if execution_gate is not None:
            execution_gate.assert_dispatch(thread_id, execution_action)
        result = self.request(
            "turn/start",
            params,
        )
        turn_id = result["turn"]["id"]
        if execution_gate is not None and execution_action in {"verify", "sync", "closeout"}:
            try:
                execution_gate.grant_cleanup_turn(thread_id, turn_id, execution_action)
            except ValueError:
                pass  # A concurrent stop is handled by the interrupt loop below.
        if on_turn_started is not None:
            on_turn_started(turn_id)
        deltas: list[str] = []
        final_answer: str | None = None
        deadline = time.monotonic() + timeout
        interrupt_sent = False
        while True:
            if time.monotonic() >= deadline:
                raise AppServerTimeout("turn completion timed out; execution state unknown")
            persistent_cancel = False
            if execution_gate is not None:
                try:
                    execution_gate.assert_dispatch(thread_id, execution_action)
                except Exception:
                    # Unreadable cancellation state is not permission to proceed.
                    persistent_cancel = True
            if (persistent_cancel or (cancel_event is not None and cancel_event.is_set())) and not interrupt_sent:
                try:
                    self.interrupt_turn(thread_id, turn_id)
                except AppServerError as exc:
                    # Acknowledgement can precede activation on the same owner.
                    # Keep the request pending and retry after the bounded event poll.
                    if "no active turn to interrupt" not in str(exc).casefold():
                        raise
                else:
                    interrupt_sent = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout("turn completion timed out; execution state unknown")
            try:
                event = self._wait_for(
                    lambda item: item.get("method") in {
                        "item/agentMessage/delta",
                        "item/completed",
                        "turn/completed",
                    },
                    min(1.0, remaining) if cancel_event is not None or execution_gate is not None else remaining,
                )
            except AppServerTimeout:
                if (cancel_event is not None or execution_gate is not None) and time.monotonic() < deadline:
                    continue
                raise
            if event["method"] == "item/agentMessage/delta":
                params = event.get("params", {})
                if params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                    deltas.append(params.get("delta", ""))
                continue
            if event["method"] == "item/completed":
                params = event.get("params", {})
                item = params.get("item", {})
                if (
                    params.get("threadId") == thread_id
                    and params.get("turnId") == turn_id
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                ):
                    final_answer = item.get("text")
                continue
            params = event.get("params", {})
            if params.get("threadId") != thread_id or params.get("turn", {}).get("id") != turn_id:
                continue
            turn = params["turn"]
            if execution_gate is not None:
                execution_gate.record_stop(thread_id, turn_id, turn["status"])
            if turn["status"] != "completed":
                error_type = AppServerTurnFailed if turn["status"] == "failed" else AppServerError
                raise error_type(f"turn ended with {turn['status']}: {turn.get('error')}")
            return final_answer or "".join(deltas).strip()

    def latest_final_answer(self, thread_id: str) -> str:
        return self.completed_final_answers(thread_id)[-1]

    def completed_final_answers(self, thread_id: str) -> list[str]:
        """Final answers of all completed turns, oldest first; the latest turn must be completed."""
        result = self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        # Never use an older successful reply to conceal the current turn's failure.
        turns = result["thread"].get("turns", [])
        for turn in turns[-1:]:
            if turn.get("status") == "failed":
                raise AppServerTurnFailed(f"thread {thread_id} turn {turn.get('id')} failed: {turn.get('error')}")
            if turn.get("status") != "completed":
                raise AppServerError(f"thread {thread_id} latest turn is {turn.get('status')}")
        answers = []
        for turn in turns:
            if turn.get("status") != "completed":
                continue
            for item in reversed(turn.get("items", [])):
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    answers.append(str(item.get("text", "")))
                    break
        if not answers:
            raise AppServerError(f"thread {thread_id} has no final answer")
        return answers

    def read_turn(self, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        """Read one exact persisted turn; absence is unknown, not completion."""
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        return next((turn for turn in result["thread"].get("turns", [])
                     if turn.get("id") == turn_id), None)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        """Request interruption. Caller must read the turn to verify it stopped."""
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def verify_stop(self, thread_id: str, turn_id: str, *, execution_gate=None) -> dict[str, Any]:
        """Read exact terminal turn; interrupt ACK, absence and errors prove nothing."""
        try:
            turn = self.read_turn(thread_id, turn_id)
        except AppServerError:
            turn = None
        status = (turn or {}).get('status')
        if status not in {'completed', 'interrupted', 'failed'}:
            return {'status': 'stop_unverified', 'owner': thread_id, 'turn_id': turn_id}
        if execution_gate is not None:
            return execution_gate.record_stop(thread_id, turn_id, status)
        return {'status': 'stop_verified', 'owner': thread_id, 'turn_id': turn_id, 'turn_status': status}

    def read_user_update_source(self, source: str) -> dict[str, Any]:
        """Lookup exact native message, without fabricating missing human provenance.

        Current UserMessageThreadItem has id/content/clientId but no authenticated
        human-vs-task-forward origin. Consequently this transport cannot yet return
        human_verified=True, even for a real userMessage. A richer trusted adapter
        can be installed by the controller when such metadata is available.
        """
        ref = json.loads(source)
        thread_id, message_id = ref['source_thread_id'], ref['source_message_id']
        result = self.request('thread/read', {'threadId': thread_id, 'includeTurns': True})
        for turn in result['thread'].get('turns', []):
            for item in turn.get('items', []):
                if item.get('id') == message_id and item.get('type') == 'userMessage':
                    return {'source_thread_id': thread_id, 'source_message_id': message_id,
                            'user_text': '\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text'),
                            'human_verified': False, 'transport': 'codex/thread/read',
                            'verification_gap': 'native schema lacks authenticated human origin'}
        return {'human_verified': False, 'verification_gap': 'source message not found'}
