from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from pipeline.store import ApplicationQueue


MAX_BATCH_URLS = 20
BATCH_CONCURRENCY = 3


class BatchItemResult(TypedDict):
    index: int
    url: str
    result: dict[str, Any]


class BatchIntakeResult(TypedDict):
    status: Literal["completed", "invalid_batch"]
    results: list[BatchItemResult]
    counts_by_status: dict[str, int]
    newly_queued: int
    reason: NotRequired[str]


class IntakeStarResult(TypedDict):
    starred: bool | None
    star_warning: NotRequired[str]


class JobfeedIntake:
    def __init__(
        self, db_path: Path, jobfeed_root: Path, jobfeed_python: Path | None = None
    ):
        self.db_path = db_path.resolve()
        self.jobfeed_root = jobfeed_root.resolve()
        self.jobfeed_python = jobfeed_python or Path(sys.executable)

    async def add_job_urls(self, urls: list[str]) -> BatchIntakeResult:
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= MAX_BATCH_URLS
            or any(not isinstance(url, str) for url in urls)
        ):
            return {
                "status": "invalid_batch",
                "results": [],
                "counts_by_status": {},
                "newly_queued": 0,
                "reason": f"Provide a list of 1 to {MAX_BATCH_URLS} URL strings",
            }

        try:
            preflight = await self._run_importer(
                ["--url-keys"], json.dumps(urls).encode()
            )
        except Exception:
            preflight = {}
        keys = preflight.get("keys")
        if (
            preflight.get("status") != "url_keys"
            or not isinstance(keys, list)
            or len(keys) != len(urls)
            or any(
                key is not None and (not isinstance(key, str) or len(key) > 9000)
                for key in keys
            )
        ):
            return {
                "status": "completed",
                "results": [
                    {
                        "index": index,
                        "url": url,
                        "result": {
                            "status": "error",
                            "reason": "URL preflight failed. No batch imports started.",
                        },
                    }
                    for index, url in enumerate(urls)
                ],
                "counts_by_status": {"error": len(urls)},
                "newly_queued": 0,
            }
        locks = {key: asyncio.Lock() for key in keys if key is not None}
        item_locks = [locks[key] if key is not None else asyncio.Lock() for key in keys]
        slots = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def import_item(index: int, url: str) -> BatchItemResult:
            async with item_locks[index], slots:
                try:
                    result = await self.add_job_url(url)
                except Exception:
                    result = {
                        "status": "error",
                        "reason": "Import failed. Save outcome may be unknown; retrying the same URL is safe.",
                    }
                return {"index": index, "url": url, "result": result}

        tasks = [
            asyncio.create_task(import_item(index, url))
            for index, url in enumerate(urls)
        ]
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        counts = dict(Counter(item["result"]["status"] for item in results))
        return {
            "status": "completed",
            "results": results,
            "counts_by_status": counts,
            "newly_queued": counts.get("queued", 0),
        }

    async def add_job_url(self, url: str) -> dict:
        if not isinstance(url, str) or not url.strip() or len(url) > 8192:
            return {
                "status": "needs_details",
                "reason": "Provide one public posting URL, at most 8192 characters",
            }
        if not self.db_path.is_file():
            return {
                "status": "error",
                "reason": "Configured Jobfeed database is missing. Run the explicit initialization command first.",
            }
        imported = await self._run_importer(["--", url])
        return await self._admit_imported(imported)

    async def add_observed_posting(self, posting: dict[str, Any]) -> dict:
        """Import a native observation and apply the ordinary queue admission rules."""
        if not self.db_path.is_file():
            return {"status": "error", "reason": "Configured Jobfeed database is missing"}
        imported = await self._run_importer(
            ["--observed-posting"], json.dumps(posting).encode()
        )
        return await self._admit_imported(imported)

    async def _admit_imported(self, imported: dict) -> dict:
        if imported["status"] not in {"imported", "existing", "closed"}:
            return imported
        job_id = imported.get("source_job_id")
        if type(job_id) is not int or job_id < 1:
            if imported["status"] == "closed":
                return imported
            return {
                "status": "error",
                "reason": "Importer returned no persisted source ID; save outcome is unknown",
            }
        star = await self._default_star_imported(job_id)
        if imported["status"] == "closed":
            return {**imported, **star}
        try:
            queue = ApplicationQueue(self.db_path)
            queue.initialize()
            result = asdict(queue.admit(job_id, manual_priority=True))
        except Exception:
            return {
                **imported,
                **star,
                "status": "imported_not_queued",
                "reason": "Posting was saved but queue admission failed; retry the same URL after repairing the queue",
            }
        return {**result, **star, "imported": imported["status"] == "imported"}

    async def _default_star_imported(self, job_id: int) -> IntakeStarResult:
        try:
            result = await self._run_importer(["--star-imported", str(job_id)])
        except (OSError, RuntimeError, ValueError):
            result = {}
        if (
            result.get("status") == "starred"
            and type(result.get("source_job_id")) is int
            and result["source_job_id"] == job_id
            and type(result.get("starred")) is bool
        ):
            return {"starred": result["starred"]}
        return {
            "starred": None,
            "star_warning": "The posting was saved, but its star update could not be verified. Queue admission is reported separately.",
        }

    def prioritize_jobs(self, job_ids: list[int]) -> dict:
        if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 100
                or any(type(job_id) is not int or job_id < 1 for job_id in job_ids)):
            return {"status": "invalid_batch", "results": [], "reason": "Provide 1 to 100 positive source job IDs"}
        if not self.db_path.is_file():
            return {"status": "error", "results": [], "reason": "Configured Jobfeed database is missing"}
        queue = ApplicationQueue(self.db_path)
        queue.initialize()
        results = [asdict(queue.admit(job_id, manual_priority=True)) for job_id in job_ids]
        return {"status": "completed", "results": results}

    async def _run_importer(
        self, arguments: list[str], payload: bytes | None = None
    ) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.jobfeed_root / "src")
        command = [
            str(self.jobfeed_python),
            "-m",
            "jobfeed.import_url",
            "--db",
            str(self.db_path),
            *arguments,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.jobfeed_root,
            env=env,
            stdin=asyncio.subprocess.PIPE if payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=55
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {
                "status": "error",
                "reason": "Import timed out. Save outcome is unknown; retrying the same URL is safe.",
            }
        try:
            imported = json.loads(stdout)
            if (
                process.returncode
                or not isinstance(imported, dict)
                or "status" not in imported
            ):
                raise ValueError("Importer did not return a valid result")
        except (ValueError, TypeError):
            return {
                "status": "error",
                "reason": "Importer failed. Save outcome is unknown; inspect the configured database and runtime before retrying.",
            }
        return imported
