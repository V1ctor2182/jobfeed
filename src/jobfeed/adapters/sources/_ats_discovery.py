"""Core of ATS board discovery: origins, the lookup cache, and the bounded runner.

An origin lists work from a public page (a VC portfolio board, a sitemap, a
Hacker News thread). Work is either a board read straight off the listing or a
``Lookup``: one more request that resolves a portfolio company to its board.
Lookups are expensive, so their answers persist in a JSON cache keyed by origin
and external id, and each run spends at most a fixed budget of them.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import chain, zip_longest
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from jobfeed.domain.errors import RunLeaseLostError


@dataclass(frozen=True, order=True)
class Board:
    """One employer job board on a supported ATS vendor."""

    slug: str
    vendor: str


@dataclass(frozen=True)
class Lookup:
    """One request's worth of work that resolves a company to its board.

    ``key`` names the company by origin and external id (for example
    ``getro:8672:202755``); it is the cache key, so it must stay stable.
    """

    key: str
    resolve: Callable[[], Awaitable[Board | None]]


@dataclass(frozen=True)
class OriginWork:
    """What one origin's listing produced."""

    listed: int
    boards: frozenset[Board] = frozenset()
    lookups: tuple[Lookup, ...] = ()


class Origin(Protocol):
    """A public listing of companies that leads to their ATS boards."""

    name: str

    async def list_work(self, http: PoliteHttp) -> OriginWork:
        """List this origin's companies; listing requests cost no budget.

        Args:
            http: Paced client for every request.

        Returns:
            Boards read off the listing and lookups for the rest.
        """


class PoliteHttp:
    """Shared client that caps concurrency and spaces requests to each host."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_concurrent: int,
        pacing_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._slots = asyncio.Semaphore(max_concurrent)
        self._pacing_s = pacing_s
        self._sleep = sleep
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_next: dict[str, float] = {}

    async def text(self, url: str) -> str:
        """GET a page and return its body.

        Args:
            url: Page URL.

        Returns:
            The response text.
        """
        response = await self._send("GET", url)
        return response.text

    async def json(self, url: str, body: dict[str, Any] | None = None) -> object:
        """GET (no body) or POST (body) a JSON API and decode the reply.

        Args:
            url: API URL.
            body: JSON request body; None sends a GET.

        Returns:
            The decoded JSON.
        """
        # Getro answers 406 without Accept; no other header is sent on purpose.
        headers = {"Accept": "application/json"}
        if body is None:
            response = await self._send("GET", url, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            response = await self._send(
                "POST", url, headers=headers, content=json.dumps(body)
            )
        return response.json()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._slots:
            await self._wait_turn(urlsplit(url).hostname or "")
            response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    async def _wait_turn(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self._host_next.get(host, 0.0) - time.monotonic()
            if delay > 0:
                await self._sleep(delay)
            self._host_next[host] = time.monotonic() + self._pacing_s


@dataclass(frozen=True)
class CacheEntry:
    """A lookup's answer: the board, or None when the company had no ATS link."""

    board: Board | None
    checked_at: datetime


class LookupCache:
    """Lookup answers persisted as readable JSON next to the database.

    Only the discovery runner writes it, and scans hold the run lease, so the
    file needs no locking; the write is atomic so a crash never truncates it.
    """

    def __init__(self, path: Path, entries: dict[str, CacheEntry]) -> None:
        self.path = path
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> LookupCache:
        """Read the cache; a missing or unreadable file starts empty.

        Args:
            path: Cache file location.

        Returns:
            The cache with every well-formed entry.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path, {})
        stored = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(stored, dict):
            return cls(path, {})
        entries = {
            key: entry
            for key, value in stored.items()
            if (entry := _parse_entry(value)) is not None
        }
        return cls(path, entries)

    def needs_lookup(self, key: str, now: datetime, recheck: timedelta) -> bool:
        """Whether a key has no answer yet or its "no board" answer went stale.

        Args:
            key: Lookup key.
            now: Current time.
            recheck: Age after which a "no board" answer is asked again.

        Returns:
            True when the lookup should run.
        """
        entry = self.entries.get(key)
        if entry is None:
            return True
        return entry.board is None and now - entry.checked_at >= recheck

    def save(self) -> None:
        """Write every entry atomically (temporary file, then rename)."""
        payload = {
            "entries": {
                key: {
                    "slug": entry.board.slug if entry.board else None,
                    "vendor": entry.board.vendor if entry.board else None,
                    "checked_at": entry.checked_at.isoformat(),
                }
                for key, entry in sorted(self.entries.items())
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)


def _parse_entry(value: object) -> CacheEntry | None:
    if not isinstance(value, dict):
        return None
    slug, vendor = value.get("slug"), value.get("vendor")
    try:
        checked_at = datetime.fromisoformat(str(value.get("checked_at")))
    except ValueError:
        return None
    if isinstance(slug, str) and isinstance(vendor, str):
        return CacheEntry(Board(slug, vendor), checked_at)
    if slug is None and vendor is None:
        return CacheEntry(None, checked_at)
    return None


@dataclass
class OriginReport:
    """One origin's outcome for a run."""

    name: str
    listed: int = 0
    cached: int = 0
    looked_up: int = 0
    lookup_errors: int = 0
    deferred: int = 0
    boards: set[Board] = field(default_factory=set)
    error: str | None = None


async def discover(  # noqa: PLR0913 - one run's independent inputs
    origins: Sequence[Origin],
    http: PoliteHttp,
    cache: LookupCache,
    *,
    max_lookups: int,
    recheck: timedelta,
    now: datetime,
) -> list[OriginReport]:
    """Collect boards from every origin, spending at most ``max_lookups``.

    A failed listing marks only its own origin; a failed lookup stays out of the
    cache so the next run asks again. The budget is shared round-robin across
    origins so no single origin starves the rest during a backfill.

    Args:
        origins: Origins to list.
        http: Paced client for every request.
        cache: Lookup answers; updated in place, not saved.
        max_lookups: Lookups allowed this run.
        recheck: Age after which a "no board" answer is asked again.
        now: Time stamped on new answers.

    Returns:
        One report per origin, in the given order.

    Complexity:
        O(L log L) for L listed lookups, plus at most ``max_lookups`` requests.
    """
    reports = [OriginReport(origin.name) for origin in origins]
    works = await asyncio.gather(
        *(
            _list(origin, http, report)
            for origin, report in zip(origins, reports, strict=True)
        )
    )
    queues: list[list[tuple[OriginReport, Lookup]]] = []
    for report, work in zip(reports, works, strict=True):
        report.listed = work.listed
        report.boards |= work.boards
        queue = []
        for lookup in work.lookups:
            if cache.needs_lookup(lookup.key, now, recheck):
                queue.append((report, lookup))
                continue
            report.cached += 1
            if board := cache.entries[lookup.key].board:
                report.boards.add(board)
        # Never-asked companies go before stale "no board" answers.
        queue.sort(key=lambda item: item[1].key in cache.entries)
        queues.append(queue)
    interleaved = [
        item for item in chain.from_iterable(zip_longest(*queues)) if item is not None
    ]
    for report, _ in interleaved[max_lookups:]:
        report.deferred += 1
    await asyncio.gather(
        *(
            _look_up(report, lookup, cache, now)
            for report, lookup in interleaved[:max_lookups]
        )
    )
    return reports


async def _list(origin: Origin, http: PoliteHttp, report: OriginReport) -> OriginWork:
    try:
        return await origin.list_work(http)
    except RunLeaseLostError:
        raise
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        return OriginWork(listed=0)


async def _look_up(
    report: OriginReport, lookup: Lookup, cache: LookupCache, now: datetime
) -> None:
    try:
        board = await lookup.resolve()
    except RunLeaseLostError:
        raise
    except Exception:
        report.lookup_errors += 1
        return
    report.looked_up += 1
    cache.entries[lookup.key] = CacheEntry(board, now)
    if board is not None:
        report.boards.add(board)


__all__ = [
    "Board",
    "CacheEntry",
    "Lookup",
    "LookupCache",
    "Origin",
    "OriginReport",
    "OriginWork",
    "PoliteHttp",
    "discover",
]
