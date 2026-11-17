"""Local dashboard adapter to the pipeline's durable submission requests."""

import asyncio
import json
import os
import secrets
import signal
import sqlite3
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from jobfeed.observability import get_logger

MAX_RESPONSE_BYTES = 2_000_000


async def _stop_reconciliation(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.communicate()


class ApplicationSubmissionError(Exception):
    """A submission command failure with an API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ApplicationSubmissionService:
    """Runs dashboard submission commands and reconciles pending requests."""

    def __init__(self, root: Path, jobfeed_db: Path):
        self.root, self.jobfeed_db = root.resolve(), jobfeed_db.resolve()
        self.csrf_token = secrets.token_urlsafe(32)

    async def run_reconciler(self, *, interval: float = 5) -> None:
        """Reconcile pending submissions forever, logging and retrying on failure.

        Args:
            interval: Seconds to wait between reconciliation passes.
        """
        while True:
            try:
                await self.reconcile_pending()
            except Exception:
                get_logger().warning("application_submission_reconciliation_deferred")
            await asyncio.sleep(interval)

    async def reconcile_pending(self, *, timeout: float = 85) -> None:
        """Run the pipeline's reconciler once when the ledger has unfinished requests.

        The reconciler process is killed if it fails, times out, or this task is
        cancelled.

        Args:
            timeout: Seconds the reconciler may run.

        Raises:
            asyncio.CancelledError: If the task is cancelled while the process starts.
            ValueError: If the reconciler's output is oversized or reports failure.
        """
        ledger = self.root / ".pipeline/dashboard-submissions.sqlite3"
        if not ledger.is_file():
            return
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=0) as db:
            if not db.execute("""SELECT 1 FROM requests WHERE
                state IN ('preparing','unknown','queued','running','sync_pending')
                OR (state IN ('blocked','turn_ended') AND prompt IS NOT NULL)
                LIMIT 1""").fetchone():
                return
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.root / ".venv/bin/python"),
                "-m",
                "pipeline.dashboard_submission",
                "--root",
                str(self.root),
                "--jobfeed-db",
                str(self.jobfeed_db),
                "reconcile-pending",
                cwd=self.root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            spawned: asyncio.subprocess.Process | None = None
            with suppress(Exception):
                spawned = await spawning
            if spawned is not None:
                await _stop_reconciliation(spawned)
            raise
        succeeded = False
        try:
            async with asyncio.timeout(timeout):
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(b"{}")
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > MAX_RESPONSE_BYTES:
                        raise ValueError("Oversized submission reconciliation response")
                await process.wait()
                result = json.loads(output)
                if process.returncode or not isinstance(result, dict):
                    raise ValueError("Submission reconciliation deferred")
                succeeded = True
        finally:
            if not succeeded:
                await _stop_reconciliation(process)

    def command(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one dashboard submission command in the pipeline.

        Args:
            command: Submission command name, such as `preview` or `confirm`.
            payload: JSON payload for the command.

        Returns:
            The command's response with the CSRF token added.

        Raises:
            ValueError: Internally, for an oversized or non-object response.
            ApplicationSubmissionError: If the command fails or its delivery cannot
                be confirmed.
        """
        try:
            result = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "pipeline.dashboard_submission",
                    "--root",
                    str(self.root),
                    "--jobfeed-db",
                    str(self.jobfeed_db),
                    command,
                ],
                input=json.dumps(payload),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=85,
                check=False,
            )
            if len(result.stdout) > MAX_RESPONSE_BYTES:
                raise ValueError("Oversized submission response")
            body = json.loads(result.stdout)
            if not isinstance(body, dict):
                raise ValueError("Invalid submission response")
            if result.returncode:
                code = body.get("code", "unavailable")
                raise ApplicationSubmissionError(
                    code
                    if code in {"invalid", "conflict", "unavailable"}
                    else "unavailable",
                    body.get("error", "Submission is unavailable."),
                )
            return {**body, "csrf_token": self.csrf_token}
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise ApplicationSubmissionError(
                "unavailable",
                "Submission delivery is unconfirmed. "
                "Check the existing request status; do not send another request.",
            ) from None
