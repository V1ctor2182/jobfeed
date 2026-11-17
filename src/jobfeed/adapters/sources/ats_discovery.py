"""Find employer ATS boards on VC portfolio boards and HN, and track the new ones.

The scan source and the ``discover-companies`` command share everything here:
one discovery run, the plan of which boards are new, and the company row each
new board becomes. Jobs come only from the ATS vendor APIs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jobfeed.adapters.sources._ats_discovery import (
    Board,
    LookupCache,
    OriginReport,
    PoliteHttp,
    discover,
)
from jobfeed.adapters.sources._ats_discovery_origins import build_origins
from jobfeed.adapters.sources.ats import ATS_DISCOVERY_NOTE, ATSSource
from jobfeed.config import SourcesATSDiscoveryConfig
from jobfeed.domain.errors import RunLeaseLostError
from jobfeed.domain.models import CompanyRecord, JobPosting
from jobfeed.observability import JobfeedLogger
from jobfeed.ports.source import PartialSourceFetchError
from jobfeed.ports.store_ops import StoreOpsMixin

CACHE_FILE_NAME = "ats_discovery_cache.json"
_STAT_KEYS = (
    "companies_listed",
    "lookups_done",
    "lookups_cached",
    "lookups_deferred",
    "lookup_errors",
    "boards_found",
    "boards_added",
    "origins_failed",
)


def discovery_cache_path(database_path: Path) -> Path:
    """Place the lookup cache beside the database, in the gitignored data folder.

    Args:
        database_path: Configured SQLite database path.

    Returns:
        The cache file path.
    """
    return database_path.parent / CACHE_FILE_NAME


async def run_discovery(
    config: SourcesATSDiscoveryConfig,
    client: httpx.AsyncClient,
    cache_path: Path,
    *,
    now: datetime,
    max_lookups: int | None = None,
) -> list[OriginReport]:
    """Run every configured origin once and save the lookup cache.

    Args:
        config: Discovery configuration.
        client: HTTP client (it carries the jobfeed User-Agent).
        cache_path: Lookup cache file.
        now: Time stamped on new lookup answers.
        max_lookups: Override of ``config.max_lookups_per_scan``.

    Returns:
        One report per origin.
    """
    cache = LookupCache.load(cache_path)
    http = PoliteHttp(
        client, max_concurrent=config.max_concurrent, pacing_s=config.pacing_s
    )
    try:
        return await discover(
            build_origins(config),
            http,
            cache,
            max_lookups=max_lookups or config.max_lookups_per_scan,
            recheck=timedelta(days=config.recheck_days),
            now=now,
        )
    finally:
        cache.save()


def new_boards(boards: set[Board], tracked: list[CompanyRecord]) -> list[Board]:
    """Return the boards whose slug no company row has, removed rows included.

    The store keys companies by slug alone, so a slug found on two vendors is
    written once: the alphabetically first vendor wins.

    Args:
        boards: Discovered boards.
        tracked: Every company row, removed ones included.

    Returns:
        New boards sorted by slug.
    """
    known = {company.slug.casefold() for company in tracked}
    by_slug: dict[str, Board] = {}
    for board in sorted(boards):
        by_slug.setdefault(board.slug.casefold(), board)
    return [board for slug, board in sorted(by_slug.items()) if slug not in known]


def discovered_company(board: Board, now: datetime) -> CompanyRecord:
    """Build the company row for a newly discovered board.

    Args:
        board: The board.
        now: Discovery time.

    Returns:
        A row pinned to the board's vendor and marked as discovered.
    """
    return CompanyRecord(
        slug=board.slug,
        ats_vendor=board.vendor,
        # Pinned: a later re-probe tries Greenhouse first and could move an
        # Ashby board to an unrelated Greenhouse namesake.
        ats_override=True,
        last_verified_at=now,
        last_probe_attempt_at=now,
        notes=ATS_DISCOVERY_NOTE,
    )


def failure_summary(reports: list[OriginReport]) -> list[str]:
    """Describe each failed listing and each origin with failed lookups.

    Args:
        reports: One run's origin reports.

    Returns:
        One line per problem; empty when nothing failed.
    """
    lines = [f"{r.name} listing failed: {r.error}" for r in reports if r.error]
    lines += [
        f"{r.name}: {r.lookup_errors} lookups failed"
        for r in reports
        if r.lookup_errors
    ]
    return lines


class ATSDiscoverySource:
    """Scan source that tracks newly discovered boards and returns their jobs.

    The ATS source runs concurrently in the same scan and has already listed
    companies, so this source fetches the boards it adds itself.
    """

    def __init__(  # noqa: PLR0913 - independently injected source collaborators
        self,
        *,
        ats: ATSSource,
        store: StoreOpsMixin,
        config: SourcesATSDiscoveryConfig,
        client: httpx.AsyncClient,
        cache_path: Path,
        logger: JobfeedLogger,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ats = ats
        self._store = store
        self._config = config
        self._client = client
        self._cache_path = cache_path
        self._log = logger
        self._now = now or (lambda: datetime.now(UTC))
        self.stats: dict[str, int] = {}

    async def fetch_jobs(self, config: dict[str, object]) -> list[JobPosting]:  # noqa: ARG002
        """Discover boards, add the new ones, and return their target-title jobs.

        Args:
            config: Protocol-satisfying no-op parameter.

        Returns:
            Target-title postings from boards added by this call.

        Raises:
            PartialSourceFetchError: Always a warning, with the postings already
                fetched, so a failed origin never fails the scan.
        """
        self.stats = dict.fromkeys(_STAT_KEYS, 0)
        postings: list[JobPosting] = []
        failures: list[str] = []
        try:
            await self._discover(postings, failures)
        except RunLeaseLostError:
            raise
        except Exception as exc:
            failures.append(f"ATS discovery failed: {exc}")
        if failures:
            self._log.warning("ats_discovery_partial", failures=failures)
            raise PartialSourceFetchError("; ".join(failures), postings, warning=True)
        return postings

    async def _discover(self, postings: list[JobPosting], failures: list[str]) -> None:
        now = self._now()
        reports = await run_discovery(
            self._config, self._client, self._cache_path, now=now
        )
        failures += failure_summary(reports)
        boards: set[Board] = set().union(*(r.boards for r in reports))
        tracked = await self._store.list_companies(include_removed=True)
        records = [discovered_company(b, now) for b in new_boards(boards, tracked)]
        self.stats |= {
            "companies_listed": sum(r.listed for r in reports),
            "lookups_done": sum(r.looked_up for r in reports),
            "lookups_cached": sum(r.cached for r in reports),
            "lookups_deferred": sum(r.deferred for r in reports),
            "lookup_errors": sum(r.lookup_errors for r in reports),
            "boards_found": len(boards),
            "origins_failed": sum(r.error is not None for r in reports),
        }
        for record in records:
            await self._store.upsert_company(record)
            self.stats["boards_added"] += 1
        if not records:
            return
        try:
            postings += await self._ats.fetch_boards(records)
        except PartialSourceFetchError as exc:
            postings += exc.postings
            failures.append(str(exc))


__all__ = [
    "CACHE_FILE_NAME",
    "ATSDiscoverySource",
    "discovered_company",
    "discovery_cache_path",
    "failure_summary",
    "new_boards",
    "run_discovery",
]
