"""a16z speedrun talent network source: paged list API plus per-job detail calls.

Strictly sequential because the API rate limits bursts. Every HTTP request is
spaced by ``pacing_s``; a job whose complete JD is already stored costs nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from jobfeed.adapters.sources._http import ATSFetchError, ATSParseError
from jobfeed.adapters.sources.jobright import (
    _datetime,
    _mapping,
    _required_text,
    _text,
)
from jobfeed.config import SourcesSpeedrunConfig
from jobfeed.config_sources import SPEEDRUN_JOBS_API_URL
from jobfeed.domain.external_identity import external_identity
from jobfeed.domain.models import JobPosting, QualityBand
from jobfeed.domain.quality import assess_quality
from jobfeed.observability import JobfeedLogger
from jobfeed.ports.source import (
    EnrichmentLookup,
    PartialSourceFetchError,
    SourceFetchProgress,
    SourceFetchProgressCallback,
    StoredEnrichment,
)

_PLATFORM = "speedrun"
_ENRICH_SOURCE = "speedrun_api"
_SOURCE_PARAM = "jobfeed"
_LIST_SLUG = "list"
_NOT_FOUND = 404
_TOO_MANY_REQUESTS = 429
_DEFAULT_RETRY_WAIT_S = 60.0
_MAX_RETRY_WAIT_S = 120.0
_COMPLETE_BANDS = frozenset({QualityBand.GOOD, QualityBand.FULL})


class SpeedrunSource:
    """Fetch speedrun talent network jobs with a complete JD per posting."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        config: SourcesSpeedrunConfig,
        logger: JobfeedLogger,
        enrichment_lookup: EnrichmentLookup | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._log = logger
        self._enrichment_lookup = enrichment_lookup
        self._on_progress: SourceFetchProgressCallback | None = None
        self._request_sent = False
        self.stats: dict[str, int] = {}

    async def fetch_jobs_with_progress(
        self, config: dict[str, object], on_progress: SourceFetchProgressCallback
    ) -> list[JobPosting]:
        """Fetch jobs while reporting list and detail progress.

        Args:
            config: Source invocation settings reserved for interface parity.
            on_progress: Callback receiving scan progress updates.

        Returns:
            Postings fetched or rebuilt from stored JDs.
        """
        self._on_progress = on_progress
        try:
            return await self.fetch_jobs(config)
        finally:
            self._on_progress = None

    async def fetch_jobs(self, config: dict[str, object]) -> list[JobPosting]:  # noqa: ARG002
        """Walk every search URL, then build one posting per distinct job.

        Args:
            config: Source invocation settings reserved for interface parity.

        Returns:
            Postings fetched or rebuilt from stored JDs.

        Raises:
            ATSFetchError: If a list page fails before any job was collected.
            PartialSourceFetchError: If a later failure left jobs unfetched. The
                next scan picks those jobs up.
        """
        self.stats = dict.fromkeys(("reused", "fetched", "closed", "failed"), 0)
        self._request_sent = False
        discovered_at = datetime.now(UTC)
        rows, error = await self._collect_rows()
        postings: list[JobPosting] = []
        if not _rate_limited(error):
            postings, detail_error = await self._build_postings(rows, discovered_at)
            error = error or detail_error
        self._log.info("speedrun_scan_finished", listed=len(rows), **self.stats)
        if error is not None:
            raise PartialSourceFetchError(
                f"speedrun scan incomplete with {len(postings)} of {len(rows)} "
                f"jobs: {error}",
                postings,
                warning=True,
            )
        return postings

    def _progress(self, phase: str, processed: int, total: int | None = None) -> None:
        if self._on_progress:
            self._on_progress(
                SourceFetchProgress(processed=processed, total=total, phase=phase)
            )

    async def _collect_rows(self) -> tuple[list[dict[str, Any]], Exception | None]:
        """Merge list rows by job id; keep what was collected if a later page fails."""
        rows: dict[str, dict[str, Any]] = {}
        error: Exception | None = None
        try:
            for search_url in self._config.search_urls:
                await self._walk_pages(search_url, rows)
        except ATSFetchError as exc:
            if not rows:
                raise
            self._log.warning(
                "speedrun_list_failed", error=str(exc), collected=len(rows)
            )
            error = exc
        return list(rows.values())[: self._config.max_jobs], error

    async def _walk_pages(
        self, search_url: str, rows: dict[str, dict[str, Any]]
    ) -> None:
        """Add one search's list rows to rows, page by page.

        Stops at max_jobs or the last page.

        Time complexity: O(P * J) for fetched pages P and rows per page J.
        """
        page = 0
        while len(rows) < self._config.max_jobs:
            payload = await self._get_json(_page_url(search_url, page), slug=_LIST_SLUG)
            jobs, total_pages = _parse_list_page(payload)
            for raw in jobs:
                # Stealth rows mask company and title, so they are not usable.
                if raw.get("stealth") is not True:
                    rows.setdefault(_text(raw["id"]), raw)
            self._progress("fetching", len(rows))
            page += 1
            if not jobs or page >= total_pages:
                return

    async def _build_postings(
        self, rows: list[dict[str, Any]], discovered_at: datetime
    ) -> tuple[list[JobPosting], Exception | None]:
        postings: list[JobPosting] = []
        error: Exception | None = None
        self._progress("details", 0, len(rows))
        for done, row in enumerate(rows, start=1):
            try:
                posting = await self._build_posting(row, discovered_at)
            except (ATSFetchError, ValueError) as exc:
                self.stats["failed"] += 1
                self._log.warning(
                    "speedrun_job_failed", job_id=_text(row["id"]), error=str(exc)
                )
                error = exc
                if _rate_limited(exc):
                    break
            else:
                if posting is not None:
                    postings.append(posting)
            self._progress("details", done, len(rows))
        return postings, error

    async def _build_posting(
        self, row: dict[str, Any], discovered_at: datetime
    ) -> JobPosting | None:
        job_id = _text(row["id"])
        stored = await self._complete_stored_jd(job_id)
        if stored is not None:
            posting = _reused_posting(row, stored, discovered_at)
            self.stats["reused"] += 1
            return posting
        try:
            payload = await self._get_json(_detail_url(job_id), slug=job_id)
        except ATSFetchError as exc:
            if exc.status_code != _NOT_FOUND:
                raise
            self.stats["closed"] += 1
            return None
        raw = _mapping(payload.get("job"), "job")
        if raw.get("status") == "closed":
            self.stats["closed"] += 1
            return None
        posting = map_speedrun_job(raw, discovered_at=discovered_at)
        self.stats["fetched"] += 1
        return posting

    async def _complete_stored_jd(self, job_id: str) -> StoredEnrichment | None:
        if self._enrichment_lookup is None:
            return None
        stored = await self._enrichment_lookup.get_enrichment(
            platform=_PLATFORM, canonical_id=job_id
        )
        if (
            stored
            and stored.quality in _COMPLETE_BANDS
            and (stored.jd_text or "").strip()
        ):
            return stored
        return None

    async def _get_json(self, url: str, *, slug: str) -> dict[str, Any]:
        """GET paced JSON, waiting out one 429 and retrying that request once."""
        try:
            response = await self._send(url)
            if response.status_code == _TOO_MANY_REQUESTS:
                wait_s = _retry_wait_s(response)
                self._log.warning("speedrun_rate_limited", wait_s=wait_s, slug=slug)
                self._progress("rate_limited", sum(self.stats.values()))
                await asyncio.sleep(wait_s)
                response = await self._send(url)
        except httpx.HTTPError as exc:
            raise ATSFetchError(
                f"Network error fetching {_PLATFORM}/{slug}: {exc}",
                slug=slug,
                vendor=_PLATFORM,
            ) from exc
        if not response.is_success:
            raise ATSFetchError(
                f"HTTP {response.status_code} from {_PLATFORM}/{slug}",
                slug=slug,
                vendor=_PLATFORM,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_error(slug, f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise _parse_error(slug, "response is not an object")
        return payload

    async def _send(self, url: str) -> httpx.Response:
        if self._request_sent:
            await asyncio.sleep(self._config.pacing_s)
        self._request_sent = True
        return await self._client.get(url)


def map_speedrun_job(raw: dict[str, Any], *, discovered_at: datetime) -> JobPosting:
    """Convert one speedrun API job object to JobPosting.

    Args:
        raw: Job object from the detail or list response.
        discovered_at: UTC timestamp assigned to the resulting posting.

    Returns:
        Normalized posting. List rows carry no description, so they map to a
        posting with quality MISSING.

    Raises:
        ValueError: If id, url, title, or company is absent.
    """
    url = _clean_url(_required_text(raw.get("url"), "url"))
    apply_url = _apply_url(raw.get("apply"), url)
    jd_text = _text(raw.get("description_text")) or None
    quality = assess_quality(jd_text)
    return JobPosting(
        platform=_PLATFORM,
        canonical_id=_required_text(raw.get("id"), "id"),
        url=url,
        apply_url=apply_url,
        external_identity=external_identity(apply_url),
        identity_evidence_url=apply_url,
        title=_required_text(raw.get("title"), "title"),
        company=_required_text(raw.get("company"), "company"),
        location=_text(raw.get("location"))
        or ("Remote" if raw.get("remote") is True else "Unknown"),
        discovered_at=discovered_at,
        jd_text=jd_text,
        jd_quality=quality,
        posted_at=_datetime(raw.get("published_at")),
        enriched_at=discovered_at if quality in _COMPLETE_BANDS else None,
        enrich_source=_ENRICH_SOURCE,
    )


def _reused_posting(
    row: dict[str, Any], stored: StoredEnrichment, discovered_at: datetime
) -> JobPosting:
    job = map_speedrun_job(row, discovered_at=discovered_at)
    apply_url = stored.apply_url or job.url
    return replace(
        job,
        jd_text=stored.jd_text,
        jd_quality=stored.quality,
        enriched_at=stored.enriched_at,
        enrich_source=stored.enrich_source,
        apply_url=apply_url,
        external_identity=stored.external_identity or external_identity(apply_url),
        identity_evidence_url=apply_url,
    )


def _apply_url(apply: object, url: str) -> str:
    if isinstance(apply, dict) and apply.get("kind") != "onsite":
        return _text(apply.get("url")) or url
    return url


def _clean_url(url: str) -> str:
    """Drop the query string, which carries utm params on list rows."""
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _page_url(search_url: str, page: int) -> str:
    parts = urlsplit(search_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"source", "page"}
    ]
    query += [("source", _SOURCE_PARAM), ("page", str(page))]
    return parts._replace(query=urlencode(query, safe=","), fragment="").geturl()


def _detail_url(job_id: str) -> str:
    return f"{SPEEDRUN_JOBS_API_URL}/{quote(job_id, safe='')}?source={_SOURCE_PARAM}"


def _parse_list_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    jobs = payload.get("jobs")
    total_pages = payload.get("total_pages")
    if (
        not isinstance(jobs, list)
        or not isinstance(total_pages, int)
        or not all(isinstance(raw, dict) and _text(raw.get("id")) for raw in jobs)
    ):
        raise _parse_error(_LIST_SLUG, "unexpected list page shape")
    return jobs, total_pages


def _parse_error(slug: str, detail: str) -> ATSParseError:
    return ATSParseError(
        f"Bad response from {_PLATFORM}/{slug}: {detail}",
        slug=slug,
        vendor=_PLATFORM,
    )


def _retry_wait_s(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "").strip()
    if not value.isdecimal():
        return _DEFAULT_RETRY_WAIT_S
    return min(float(value), _MAX_RETRY_WAIT_S)


def _rate_limited(error: Exception | None) -> bool:
    return isinstance(error, ATSFetchError) and error.status_code == _TOO_MANY_REQUESTS


__all__ = ["SpeedrunSource", "map_speedrun_job"]
