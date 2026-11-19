"""Resolve selections for queue admission and explicit controller handoffs."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from jobfeed.services.application_queue_models import (
    ApplicationHandoffRequest,
    ApplicationHandoffState,
    ApplicationQueueReceipt,
    ApplicationQueueRequest,
    ApplicationQueueState,
    CanonicalHandoffSelection,
    SourceHandoffSelection,
)

_RESPONSE_LIMIT = 2 * 1024 * 1024
_COMMAND_TIMEOUT = 55


class ApplicationQueueError(Exception):
    """A preparation queue failure with an API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class QueueStore(Protocol):
    """Store lookups the preparation queue needs."""

    async def get_real_job_view(self, real_job_id: str) -> dict[str, object] | None:
        """Read one canonical real job.

        Args:
            real_job_id: Canonical real job ID.

        Returns:
            The real job's fields, or None when it does not exist.
        """

    async def resolve_real_job_id(self, source_job_id: str) -> str | None:
        """Find the canonical real job for a source job.

        Args:
            source_job_id: Source job ID.

        Returns:
            The real job ID, or None when the source job has none.
        """


class ApplicationQueueService:
    """Admits jobs to the pipeline's preparation queue and reads its status."""

    def __init__(self, root: Path, jobfeed_db: Path, store: QueueStore) -> None:
        self.root = root.resolve()
        self.jobfeed_db = jobfeed_db.resolve()
        self.store = store
        self.csrf_token = secrets.token_urlsafe(32)

    async def _command(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.root / ".venv/bin/python"),
                "-m",
                "pipeline.dashboard_queue",
                "--root",
                str(self.root),
                "--jobfeed-db",
                str(self.jobfeed_db),
                command,
                cwd=self.root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode()),
                timeout=_COMMAND_TIMEOUT,
            )
            if len(stdout) > _RESPONSE_LIMIT:
                raise ValueError("Oversized queue response")
            body = json.loads(stdout)
            if not isinstance(body, dict):
                raise ValueError("Invalid queue response")
            if process.returncode:
                code = body.get("code", "unavailable")
                raise ApplicationQueueError(
                    code
                    if code in {"invalid", "conflict", "unavailable"}
                    else "unavailable",
                    body.get("error", "Application preparation is unavailable."),
                )
            return body
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            raise ApplicationQueueError(
                "unavailable",
                "Could not verify the queue result. "
                "Refresh this request's status before trying again.",
            ) from exc
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

    async def state(self, request_id: str | None = None) -> ApplicationQueueState:
        """Read the preparation queue status.

        Args:
            request_id: Limit the status to this request, or None for all.

        Returns:
            Queue requests, consumer status and the CSRF token.

        Raises:
            ApplicationQueueError: If the queue returns an invalid status.
        """
        body = await self._command(
            "status", {"request_id": request_id} if request_id else {}
        )
        try:
            return ApplicationQueueState.model_validate(
                {**body, "csrf_token": self.csrf_token}
            )
        except ValueError as exc:
            raise ApplicationQueueError(
                "unavailable", "The preparation queue returned an invalid status."
            ) from exc

    async def request(
        self, body: ApplicationQueueRequest, origin: str
    ) -> ApplicationQueueReceipt:
        """Queue canonical real jobs for preparation.

        Args:
            body: Request ID and the real job IDs to queue.
            origin: Origin of the dashboard request, recorded as provenance.

        Returns:
            The receipt for the queue request.
        """
        return await self._admit(
            "enqueue",
            str(body.request_id),
            CanonicalHandoffSelection(kind="canonical", real_job_ids=body.real_job_ids),
            {"provenance": self._provenance(origin)},
        )

    async def handoff(
        self, body: ApplicationHandoffRequest, origin: str, backend_origin: str
    ) -> ApplicationQueueReceipt:
        """Hand selected jobs to an Applier runtime with preparation authorized.

        Args:
            body: Request ID, job selection and runtime.
            origin: Origin of the dashboard request, recorded as provenance.
            backend_origin: Origin the Applier should use to reach Jobfeed.

        Returns:
            The receipt for the handoff request.
        """
        return await self._admit(
            "handoff",
            str(body.request_id),
            body.selection,
            {
                "authorize_preparation": True,
                "provenance": self._provenance(origin),
                "backend_origin": backend_origin,
                "project_root": str(self.root.parent),
                "runtime": body.runtime,
            },
        )

    async def handoff_state(self, request_id: str) -> ApplicationHandoffState:
        """Read the status of one handoff request.

        Args:
            request_id: ID of the handoff request.

        Returns:
            The handoff's receipt, or an empty state when the request is unknown.

        Raises:
            ApplicationQueueError: If the request exists but is not a handoff.
        """
        state = await self.state(request_id)
        receipt = next(
            (item for item in state.requests if item.request_id == request_id), None
        )
        if receipt is not None and receipt.intent != "handoff":
            raise ApplicationQueueError("conflict", "This request is not a handoff.")
        return ApplicationHandoffState(request=receipt)

    @staticmethod
    def _provenance(origin: str) -> dict[str, str]:
        return {
            "kind": "local_dashboard",
            "origin": origin,
            "received_at": datetime.now(UTC).isoformat(),
        }

    async def _admit(  # noqa: C901 - one admission transaction; splitting it hides the order of checks
        self,
        command: str,
        request_id: str,
        selection: CanonicalHandoffSelection | SourceHandoffSelection,
        metadata: dict[str, Any],
    ) -> ApplicationQueueReceipt:
        source_selection = isinstance(selection, SourceHandoffSelection)
        selected: list[int | str]
        if isinstance(selection, SourceHandoffSelection):
            selected = list(dict.fromkeys(selection.source_job_ids))
        else:
            selected = list(dict.fromkeys(selection.real_job_ids))
        previous = await self.state(request_id)
        existing = next(
            (item for item in previous.requests if item.request_id == request_id), None
        )
        if existing is not None:
            saved = {
                item.source_job_id if source_selection else item.real_job_id
                for item in existing.items
            }
            if existing.intent != ("queue" if command == "enqueue" else "handoff"):
                raise ApplicationQueueError(
                    "conflict", "This request ID belongs to a different action."
                )
            if saved != set(selected):
                raise ApplicationQueueError(
                    "conflict", "This request ID belongs to a different selection."
                )
            if command == "handoff" and existing.runtime != metadata["runtime"]:
                raise ApplicationQueueError(
                    "conflict", "This request ID belongs to a different runtime."
                )
            return existing
        items, rejected = [], []
        canonical_ids = set()
        source_id: int | None
        for selected_id in selected:
            if source_selection:
                source_id = int(selected_id)
                real_job_id, reason = await self._resolve_source(source_id)
            else:
                real_job_id = str(selected_id)
                source_id, reason = await self._resolve(real_job_id)
            if not reason and real_job_id in canonical_ids:
                reason = "Another selected source already represents this job."
            row = {"real_job_id": real_job_id, "source_job_id": source_id}
            if reason:
                rejected.append({**row, "reason": reason})
            else:
                items.append(row)
                canonical_ids.add(real_job_id)
        result = await self._command(
            command,
            {
                "request_id": request_id,
                "items": items,
                "rejected_items": rejected,
                **metadata,
            },
        )
        try:
            return ApplicationQueueReceipt.model_validate(result)
        except ValueError as exc:
            raise ApplicationQueueError(
                "unavailable",
                "Could not verify the queue receipt. "
                "Refresh this request's status before trying again.",
            ) from exc

    async def _resolve(self, real_job_id: str) -> tuple[int | None, str | None]:
        if not real_job_id.isdecimal() or int(real_job_id) <= 0:
            return None, "The selected canonical job ID is invalid."
        view = await self.store.get_real_job_view(real_job_id)
        if view is None:
            return None, "The selected job no longer exists."
        row = view.get("row")
        if not isinstance(row, dict) or str(row.get("real_job_id")) != real_job_id:
            return None, "The canonical job identity could not be verified."
        if row.get("status") != "shortlisted":
            return None, "Only jobs currently in Wait can be prepared."
        if row.get("closed_at"):
            return None, "The selected job is closed."
        sources = view.get("sources")
        if not isinstance(sources, list):
            return None, "No eligible source posting is available."
        candidates = sorted(
            (source for source in sources if isinstance(source, dict)),
            key=lambda source: source.get("job_id") != row.get("id"),
        )
        for source in candidates:
            source_id = source.get("job_id")
            if self._eligible_source(source):
                return source_id, None
        return None, "No open source posting with an application URL is available."

    @staticmethod
    def _eligible_source(source: dict[str, Any]) -> bool:
        try:
            url = urlsplit(str(source.get("apply_url") or source.get("url") or ""))
        except ValueError:
            return False
        source_id = source.get("job_id")
        return (
            not source.get("closed_at")
            and url.scheme in {"http", "https"}
            and bool(url.hostname)
            and type(source_id) is int
            and source_id > 0
        )

    async def _resolve_source(self, source_id: int) -> tuple[str | None, str | None]:
        try:
            real_job_id = await self.store.resolve_real_job_id(str(source_id))
        except ValueError:
            return None, "The source's canonical identity could not be verified."
        if real_job_id is None:
            return None, "The selected source posting no longer exists."
        view = await self.store.get_real_job_view(real_job_id)
        row = view.get("row") if view else None
        if not isinstance(row, dict) or str(row.get("real_job_id")) != real_job_id:
            return real_job_id, "The canonical job identity could not be verified."
        if row.get("closed_at") or row.get("status") not in {
            "new",
            "scored",
            "shortlisted",
            "awaiting_referral",
        }:
            return (
                real_job_id,
                "The selected job is no longer eligible for preparation.",
            )
        sources = view.get("sources", []) if view else []
        if not isinstance(sources, list):
            return real_job_id, "The selected source is unavailable."
        source = next(
            (
                item
                for item in sources
                if isinstance(item, dict) and item.get("job_id") == source_id
            ),
            None,
        )
        if source is None or not self._eligible_source(source):
            return real_job_id, "The selected source is closed or unavailable."
        try:
            with closing(
                sqlite3.connect(self.jobfeed_db.as_uri() + "?mode=ro", uri=True)
            ) as db:
                queued = db.execute(
                    "SELECT state FROM application_queue WHERE job_id=?", (source_id,)
                ).fetchone()
        except sqlite3.Error:
            return (
                real_job_id,
                "The selected source's queue state could not be verified.",
            )
        if not queued or queued[0] != "Queued":
            return real_job_id, "The selected source is no longer queued."
        return real_job_id, None
