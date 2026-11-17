"""ATS board discovery: origin parsers, the lookup cache, the scan source, the CLI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from click.testing import CliRunner
from pydantic import ValidationError

from jobfeed.adapters.sources import _ats_discovery
from jobfeed.adapters.sources._ats_discovery import (
    Board,
    Lookup,
    LookupCache,
    OriginWork,
    PoliteHttp,
    discover,
)
from jobfeed.adapters.sources._ats_discovery_origins import (
    A16Z_SITEMAP_URL,
    HN_ITEM_URL,
    HN_SEARCH_URL,
    a16z_company_pages,
    first_board,
    getro_companies,
    getro_job_urls,
    getro_network_id,
    hiring_thread_ids,
    hn_thread_text,
    most_linked_board,
)
from jobfeed.adapters.sources._bootstrap_aggregators import extract_ats_slugs
from jobfeed.adapters.sources.ats import ATS_DISCOVERY_NOTE, ATSSource
from jobfeed.adapters.sources.ats_discovery import (
    CACHE_FILE_NAME,
    ATSDiscoverySource,
)
from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.cli import discover as discover_module
from jobfeed.cli.discover import discover_companies
from jobfeed.config import (
    Settings,
    SourcesATSConfig,
    SourcesATSDiscoveryConfig,
    SourcesConfig,
)
from jobfeed.domain.models import CompanyRecord
from jobfeed.ports.source import PartialSourceFetchError
from tests.unit.test_ats_search_source import ASHBY, GREENHOUSE, LEVER, _payload

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
TARGET_TITLE = "Software Engineer"
OTHER_TITLE = "Account Executive"
GETRO_BOARD = "https://jobs.vc.example"
GETRO_NETWORK = 77
GETRO_API = f"https://api.getro.com/api/v2/collections/{GETRO_NETWORK}/search/"
HTTP_ERROR = 500


def next_data(network: object) -> str:
    data = {"props": {"pageProps": {"network": network}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script></html>"
    )


HN_SEARCH = {
    "hits": [
        {"objectID": "400", "title": "Ask HN: Who wants to be hired? (November 2026)"},
        {"objectID": "300", "title": "Ask HN: Who is hiring? (November 2026)"},
        {"objectID": "250", "title": "Ask HN: Freelancer? Seeking freelancer?"},
        {"objectID": "200", "title": "Ask HN: Who is hiring? (October 2026)"},
        {"objectID": "100", "title": "Ask HN: Who is hiring? (September 2026)"},
    ]
}

HN_ITEM = {
    "id": 300,
    "text": None,
    "children": [
        {
            "text": (
                '<p>Acme | Remote | <a href="https:&#x2F;&#x2F;jobs.ashbyhq.com'
                '&#x2F;acme&#x2F;123">apply</a>'
            ),
            "children": [
                {"text": "Also https:&#x2F;&#x2F;jobs.lever.co&#x2F;NestedCo"},
            ],
        },
        {"text": None, "children": None},
        {"text": "Beta https:&#x2F;&#x2F;boards.greenhouse.io&#x2F;beta"},
    ],
}


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestGetroParsers:
    def test_network_id_comes_from_next_data(self) -> None:
        page = next_data({"id": GETRO_NETWORK, "label": "Accel"})

        assert getro_network_id(page) == GETRO_NETWORK

    def test_network_id_served_as_a_string_is_read_as_a_number(self) -> None:
        assert getro_network_id(next_data({"id": str(GETRO_NETWORK)})) == GETRO_NETWORK

    @pytest.mark.parametrize(
        "page", ["<html></html>", next_data({"label": "no id"}), next_data("x")]
    )
    def test_page_without_network_id_is_an_error(self, page: str) -> None:
        with pytest.raises(ValueError, match=r"network|__NEXT_DATA__"):
            getro_network_id(page)

    def test_company_page_lists_companies_and_total(self) -> None:
        payload = {
            "results": {
                "companies": [
                    {"id": 202755, "name": "100ms", "active_jobs_count": 10},
                    {"id": 9, "name": "Quiet", "active_jobs_count": 0},
                    {"name": "no id"},
                ],
                "count": 713,
            }
        }

        companies, count = getro_companies(payload)

        assert [(c.id, c.name, c.active_jobs) for c in companies] == [
            (202755, "100ms", 10),
            (9, "Quiet", 0),
        ]
        assert count == 713  # noqa: PLR2004

    @pytest.mark.parametrize("payload", [None, {}, {"results": {"companies": "x"}}])
    def test_company_page_of_another_shape_is_an_error(self, payload: object) -> None:
        with pytest.raises(ValueError, match=r"results\.companies"):
            getro_companies(payload)

    def test_first_ats_job_url_decides_the_board(self) -> None:
        payload = {
            "results": {
                "jobs": [
                    {"url": "https://careers.example.com/jobs/1"},
                    {"url": "https://jobs.lever.co/100ms/534b"},
                    {"url": "https://jobs.ashbyhq.com/other/1"},
                ],
                "count": 3,
            }
        }

        assert first_board(getro_job_urls(payload)) == Board("100ms", "lever")

    def test_jobs_without_ats_link_resolve_to_nothing(self) -> None:
        payload = {"results": {"jobs": [{"url": "https://myworkdayjobs.com/x"}]}}

        assert first_board(getro_job_urls(payload)) is None
        assert getro_job_urls({"results": {}}) == []


class TestA16zParsers:
    def test_sitemap_keeps_one_page_per_company_and_skips_index_pages(self) -> None:
        sitemap = "".join(
            f"<url><loc>https://jobs.a16z.com{path}</loc></url>"
            for path in (
                "/",
                "/companies",
                "/jobs",
                "/company/patch",
                "/jobs/patch",
                "/company/eka-robotics",
                "/jobs/harvey",
                "/jobs/harvey/123",
            )
        )

        assert a16z_company_pages(sitemap) == {
            "patch": "https://jobs.a16z.com/jobs/patch",
            "eka-robotics": "https://jobs.a16z.com/company/eka-robotics",
            "harvey": "https://jobs.a16z.com/jobs/harvey",
        }

    def test_page_board_is_the_most_linked_one(self) -> None:
        page = (
            "https://jobs.ashbyhq.com/Valon/1 https://jobs.ashbyhq.com/valon/2 "
            "https://jobs.lever.co/partner/9"
        )

        assert most_linked_board(page) == Board("valon", "ashby")

    def test_page_without_ats_link_has_no_board(self) -> None:
        assert most_linked_board("<a href='https://example.com/careers'>") is None


class TestHackerNewsParsers:
    def test_newest_hiring_threads_skip_other_whoishiring_posts(self) -> None:
        assert hiring_thread_ids(HN_SEARCH, 2) == ["300", "200"]
        assert hiring_thread_ids(HN_SEARCH, 3) == ["300", "200", "100"]

    @pytest.mark.parametrize("search", [{"hits": []}, {"hits": "x"}, {}, [], None])
    def test_search_without_threads_yields_none(self, search: object) -> None:
        assert hiring_thread_ids(search, 3) == []

    def test_escaped_nested_comments_yield_ats_boards(self) -> None:
        assert extract_ats_slugs(hn_thread_text(HN_ITEM)) == {
            ("acme", "ashby"),
            ("nestedco", "lever"),
            ("beta", "greenhouse"),
        }

    @pytest.mark.parametrize("item", [None, [], "text", {"children": "x"}])
    def test_garbage_item_is_empty_text(self, item: object) -> None:
        assert hn_thread_text(item) == ""


# ---------------------------------------------------------------------------
# Lookup cache and budget
# ---------------------------------------------------------------------------


@dataclass
class FakeOrigin:
    """Origin whose lookups answer from a table and count their calls."""

    name: str
    answers: dict[str, Board | None]
    calls: list[str] = field(default_factory=list)
    broken: set[str] = field(default_factory=set)

    async def list_work(self, http: PoliteHttp) -> OriginWork:  # noqa: ARG002
        return OriginWork(
            listed=len(self.answers),
            lookups=tuple(
                Lookup(key=key, resolve=partial(self._resolve, key))
                for key in self.answers
            ),
        )

    async def _resolve(self, key: str) -> Board | None:
        self.calls.append(key)
        if key in self.broken:
            raise httpx.ConnectError("offline")
        return self.answers[key]


class FailingOrigin:
    name = "down"

    async def list_work(self, http: PoliteHttp) -> OriginWork:  # noqa: ARG002
        raise httpx.ConnectError("listing offline")


@pytest_asyncio.fixture
async def polite() -> AsyncIterator[PoliteHttp]:
    transport = httpx.MockTransport(lambda _: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        yield PoliteHttp(client, max_concurrent=4, pacing_s=0)


async def run(
    origins: list[Any],
    http: PoliteHttp,
    cache: LookupCache,
    *,
    now: datetime = NOW,
    max_lookups: int = 100,
) -> list[_ats_discovery.OriginReport]:
    return await discover(
        origins,
        http,
        cache,
        max_lookups=max_lookups,
        recheck=timedelta(days=30),
        now=now,
    )


async def test_resolved_lookup_is_never_asked_again(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    origin = FakeOrigin("vc", {"vc:1": Board("acme", "ashby")})
    cache = LookupCache.load(tmp_path / CACHE_FILE_NAME)

    await run([origin], polite, cache)
    cache.save()
    reloaded = LookupCache.load(tmp_path / CACHE_FILE_NAME)
    [report] = await run([origin], polite, reloaded, now=NOW + timedelta(days=400))

    assert origin.calls == ["vc:1"]
    assert report.boards == {Board("acme", "ashby")}
    assert report.cached == 1


async def test_company_without_board_is_rechecked_only_after_recheck_days(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    origin = FakeOrigin("vc", {"vc:1": None})
    cache = LookupCache.load(tmp_path / CACHE_FILE_NAME)

    await run([origin], polite, cache)
    await run([origin], polite, cache, now=NOW + timedelta(days=29))
    assert origin.calls == ["vc:1"]

    await run([origin], polite, cache, now=NOW + timedelta(days=30))
    assert origin.calls == ["vc:1", "vc:1"]


async def test_budget_stops_lookups_and_the_rest_runs_next_time(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    answers: dict[str, Board | None] = {
        f"vc:{n}": Board(f"co{n}", "lever") for n in range(5)
    }
    origin = FakeOrigin("vc", answers)
    cache = LookupCache.load(tmp_path / CACHE_FILE_NAME)

    [first] = await run([origin], polite, cache, max_lookups=2)
    [second] = await run([origin], polite, cache, max_lookups=10)

    assert (first.looked_up, first.deferred, len(first.boards)) == (2, 3, 2)
    assert (second.looked_up, second.cached, len(second.boards)) == (3, 2, 5)
    assert sorted(origin.calls) == sorted(answers)


async def test_budget_is_shared_across_origins(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    left = FakeOrigin("left", {f"l:{n}": None for n in range(3)})
    right = FakeOrigin("right", {f"r:{n}": None for n in range(3)})

    await run(
        [left, right],
        polite,
        LookupCache.load(tmp_path / CACHE_FILE_NAME),
        max_lookups=2,
    )

    assert (len(left.calls), len(right.calls)) == (1, 1)


async def test_failed_lookup_is_not_cached_and_retries_next_run(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    origin = FakeOrigin("vc", {"vc:1": Board("acme", "ashby")}, broken={"vc:1"})
    cache = LookupCache.load(tmp_path / CACHE_FILE_NAME)

    [report] = await run([origin], polite, cache)
    origin.broken.clear()
    [retry] = await run([origin], polite, cache)

    assert (report.lookup_errors, report.boards) == (1, set())
    assert retry.boards == {Board("acme", "ashby")}


async def test_failed_listing_marks_only_its_origin(
    tmp_path: Path, polite: PoliteHttp
) -> None:
    origin = FakeOrigin("vc", {"vc:1": Board("acme", "ashby")})

    down, up = await run(
        [FailingOrigin(), origin], polite, LookupCache.load(tmp_path / "c.json")
    )

    assert down.error is not None
    assert "listing offline" in down.error
    assert (up.error, up.boards) == (None, {Board("acme", "ashby")})


async def test_requests_to_one_host_are_spaced_by_pacing() -> None:
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=transport) as client:
        http = PoliteHttp(client, max_concurrent=4, pacing_s=60, sleep=record)
        await http.text("https://a.example/1")
        await http.text("https://b.example/1")
        await http.text("https://a.example/2")

    assert len(waits) == 1
    assert 59 < waits[0] <= 60  # noqa: PLR2004


def test_cache_file_is_readable_json_keyed_by_origin_and_id(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILE_NAME
    cache = LookupCache.load(path)
    cache.entries["getro:8672:202755"] = _ats_discovery.CacheEntry(
        Board("100ms", "lever"), NOW
    )
    cache.entries["a16z:quiet"] = _ats_discovery.CacheEntry(None, NOW)

    cache.save()

    assert json.loads(path.read_text()) == {
        "entries": {
            "a16z:quiet": {
                "slug": None,
                "vendor": None,
                "checked_at": NOW.isoformat(),
            },
            "getro:8672:202755": {
                "slug": "100ms",
                "vendor": "lever",
                "checked_at": NOW.isoformat(),
            },
        }
    }
    assert [p.name for p in tmp_path.iterdir()] == [CACHE_FILE_NAME]


def test_interrupted_save_leaves_previous_cache_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / CACHE_FILE_NAME
    cache = LookupCache.load(path)
    cache.entries["vc:1"] = _ats_discovery.CacheEntry(Board("acme", "ashby"), NOW)
    cache.save()
    cache.entries["vc:2"] = _ats_discovery.CacheEntry(None, NOW)

    def crash(*_: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr("jobfeed.adapters.sources._ats_discovery.os.replace", crash)
    with pytest.raises(OSError, match="disk gone"):
        cache.save()

    assert set(LookupCache.load(path).entries) == {"vc:1"}


@pytest.mark.parametrize("text", ["not json", "[]", '{"entries": 3}'])
def test_unreadable_cache_starts_empty(tmp_path: Path, text: str) -> None:
    path = tmp_path / CACHE_FILE_NAME
    path.write_text(text)

    assert LookupCache.load(path).entries == {}


# ---------------------------------------------------------------------------
# Fake public web + ATS vendor APIs
# ---------------------------------------------------------------------------


@dataclass
class FakeWeb:
    """Getro, a16z, HN, and ATS vendor APIs behind one mock transport."""

    getro_companies: list[dict[str, Any]] = field(default_factory=list)
    getro_jobs: dict[int, list[str]] = field(default_factory=dict)
    pages: dict[str, str] = field(default_factory=dict)
    json_pages: dict[str, object] = field(default_factory=dict)
    vendor_boards: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    down: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    getro_page_size: int = 2

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if any(url.startswith(prefix) for prefix in self.down):
            return httpx.Response(HTTP_ERROR)
        if url.startswith(GETRO_API):
            return self._getro(url.removeprefix(GETRO_API), json.loads(request.content))
        for base in (ASHBY, GREENHOUSE, LEVER):
            if url.startswith(base):
                slug = url.removeprefix(base).split("?")[0].removesuffix("/jobs")
                titles = self.vendor_boards.get((base, slug))
                if titles is None:
                    return httpx.Response(404)
                return httpx.Response(200, json=_payload(base, slug, titles))
        if url in self.json_pages:
            return httpx.Response(200, json=self.json_pages[url])
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url])
        return httpx.Response(404)

    def _getro(self, kind: str, body: dict[str, Any]) -> httpx.Response:
        if kind == "companies":
            start = body["page"] * self.getro_page_size
            rows = self.getro_companies[start : start + self.getro_page_size]
            count = len(self.getro_companies)
            return httpx.Response(
                200, json={"results": {"companies": rows, "count": count}}
            )
        [company_id] = body["filters"]["organization.id"]
        jobs = [{"url": url} for url in self.getro_jobs.get(company_id, [])]
        return httpx.Response(200, json={"results": {"jobs": jobs, "count": len(jobs)}})


def portfolio_web() -> FakeWeb:
    """Three origins: Getro (two hiring companies), a16z (one), HN (two boards)."""
    web = FakeWeb()
    web.pages[f"{GETRO_BOARD}/jobs"] = next_data({"id": str(GETRO_NETWORK)})
    web.getro_companies = [
        {"id": 1, "name": "Acme", "active_jobs_count": 3},
        {"id": 2, "name": "Workday Co", "active_jobs_count": 1},
        {"id": 3, "name": "Quiet", "active_jobs_count": 0},
        {"id": 4, "name": "Tracked", "active_jobs_count": 2},
    ]
    web.getro_jobs = {
        1: ["https://jobs.ashbyhq.com/acme/1"],
        2: ["https://wd5.myworkdayjobs.com/workdayco/job/1"],
        4: ["https://boards.greenhouse.io/tracked/jobs/1"],
    }
    web.pages[A16Z_SITEMAP_URL] = (
        "<urlset><url><loc>https://jobs.a16z.com/</loc></url>"
        "<url><loc>https://jobs.a16z.com/jobs/harvey</loc></url></urlset>"
    )
    web.pages["https://jobs.a16z.com/jobs/harvey"] = (
        "https://jobs.ashbyhq.com/harvey/1 https://jobs.ashbyhq.com/harvey/2"
    )
    web.json_pages[HN_SEARCH_URL] = {
        "hits": [{"objectID": "300", "title": "Ask HN: Who is hiring? (Nov)"}]
    }
    web.json_pages[HN_ITEM_URL.format(item_id="300")] = {
        "children": [
            {"text": "https:&#x2F;&#x2F;jobs.lever.co&#x2F;hnco&#x2F;1"},
            {"text": "https:&#x2F;&#x2F;jobs.lever.co&#x2F;gone&#x2F;1"},
        ]
    }
    web.vendor_boards = {
        (ASHBY, "acme"): [TARGET_TITLE, OTHER_TITLE],
        (ASHBY, "harvey"): [TARGET_TITLE],
        (LEVER, "hnco"): [OTHER_TITLE],
        (GREENHOUSE, "tracked"): [TARGET_TITLE],
    }
    return web


def discovery_config(**overrides: Any) -> SourcesATSDiscoveryConfig:
    fields: dict[str, Any] = {
        "enabled": True,
        "getro_boards": [GETRO_BOARD],
        "a16z": True,
        "hn_threads": 1,
        "pacing_s": 0,
    }
    return SourcesATSDiscoveryConfig(**(fields | overrides))


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    selected = SQLiteStore(tmp_path / "ats-discovery.sqlite3")
    await selected.connect()
    try:
        yield selected
    finally:
        await selected.close()


@pytest_asyncio.fixture
async def web() -> AsyncIterator[tuple[FakeWeb, httpx.AsyncClient]]:
    fake = portfolio_web()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fake), headers={"User-Agent": "jobfeed/1.0"}
    ) as client:
        yield fake, client


def build_source(
    store: SQLiteStore, client: httpx.AsyncClient, cache_path: Path, **config: Any
) -> ATSDiscoverySource:
    return ATSDiscoverySource(
        ats=ATSSource(
            client=client,
            store=store,
            config=SourcesATSConfig(title_keywords=[TARGET_TITLE]),
            logger=MagicMock(),
            now=lambda: NOW,
        ),
        store=store,
        config=discovery_config(**config),
        client=client,
        cache_path=cache_path,
        logger=MagicMock(),
        now=lambda: NOW,
    )


async def test_source_adds_new_boards_and_returns_only_their_target_jobs(
    tmp_path: Path, store: SQLiteStore, web: tuple[FakeWeb, httpx.AsyncClient]
) -> None:
    _, client = web
    await store.upsert_company(CompanyRecord(slug="tracked", ats_vendor="greenhouse"))
    await store.upsert_company(CompanyRecord(slug="gone", ats_vendor="lever"))
    await store.mark_company_removed("gone")
    source = build_source(store, client, tmp_path / CACHE_FILE_NAME)

    jobs = await source.fetch_jobs({})

    assert sorted((job.platform, job.company) for job in jobs) == [
        ("ashby", "acme"),
        ("ashby", "harvey"),
    ]
    assert all(job.title == TARGET_TITLE for job in jobs)
    added = {
        c.slug: c
        for c in await store.list_companies(include_removed=True)
        if c.notes == ATS_DISCOVERY_NOTE
    }
    assert {(slug, c.ats_vendor) for slug, c in added.items()} == {
        ("acme", "ashby"),
        ("harvey", "ashby"),
        ("hnco", "lever"),
    }
    assert all(c.ats_override and c.last_verified_at == NOW for c in added.values())
    assert source.stats["boards_added"] == 3  # noqa: PLR2004
    assert source.stats["origins_failed"] == 0


async def test_second_scan_looks_up_nothing_and_adds_nothing(
    tmp_path: Path, store: SQLiteStore, web: tuple[FakeWeb, httpx.AsyncClient]
) -> None:
    fake, client = web
    cache_path = tmp_path / CACHE_FILE_NAME
    await build_source(store, client, cache_path).fetch_jobs({})
    fake.requests.clear()

    second = build_source(store, client, cache_path)
    jobs = await second.fetch_jobs({})

    lookups = [r for r in fake.requests if str(r.url).endswith("/search/jobs")] + [
        r for r in fake.requests if str(r.url) == "https://jobs.a16z.com/jobs/harvey"
    ]
    assert (jobs, lookups) == ([], [])
    assert second.stats["boards_added"] == 0


async def test_one_failing_origin_is_a_warning_with_the_others_jobs(
    tmp_path: Path, store: SQLiteStore, web: tuple[FakeWeb, httpx.AsyncClient]
) -> None:
    fake, client = web
    fake.down.add(A16Z_SITEMAP_URL)
    source = build_source(store, client, tmp_path / CACHE_FILE_NAME)

    with pytest.raises(PartialSourceFetchError) as caught:
        await source.fetch_jobs({})

    assert caught.value.warning is True
    assert "a16z listing failed" in str(caught.value)
    assert sorted(job.company for job in caught.value.postings) == ["acme", "tracked"]
    assert await store.get_company("harvey") is None


async def test_getro_requests_carry_only_the_jobfeed_headers(
    tmp_path: Path, store: SQLiteStore, web: tuple[FakeWeb, httpx.AsyncClient]
) -> None:
    fake, client = web

    await build_source(store, client, tmp_path / CACHE_FILE_NAME).fetch_jobs({})

    getro = [r for r in fake.requests if str(r.url).startswith(GETRO_API)]
    assert getro
    for request in getro:
        assert request.headers["User-Agent"] == "jobfeed/1.0"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert not {"origin", "referer", "cookie"} & set(request.headers.keys())


async def test_getro_listing_pages_until_the_count_is_reached(
    tmp_path: Path, store: SQLiteStore, web: tuple[FakeWeb, httpx.AsyncClient]
) -> None:
    fake, client = web
    source = build_source(
        store, client, tmp_path / CACHE_FILE_NAME, a16z=False, hn_threads=0
    )

    await source.fetch_jobs({})

    pages = [
        json.loads(r.content)["page"]
        for r in fake.requests
        if str(r.url) == f"{GETRO_API}companies"
    ]
    assert pages == [0, 1]
    assert source.stats["companies_listed"] == 4  # noqa: PLR2004
    # Quiet has no open jobs, so only three companies cost a lookup.
    assert source.stats["lookups_done"] == 3  # noqa: PLR2004


# ---------------------------------------------------------------------------
# CLI: discover-companies
# ---------------------------------------------------------------------------


def cli_app(tmp_path: Path, store: AsyncMock) -> dict[str, Any]:
    settings = Settings.model_validate(
        {
            "db": {"path": str(tmp_path / "jobfeed.sqlite")},
            "sources": {
                "ats_discovery": {
                    "getro_boards": [GETRO_BOARD],
                    "a16z": True,
                    "hn_threads": 1,
                    "pacing_s": 0,
                }
            },
        }
    )
    return {
        "settings": settings,
        "store": store,
        "sources": {},
        "scan_service": MagicMock(),
        "digest_service": MagicMock(),
        "logger": MagicMock(),
        "verbose": False,
    }


def fake_store(*tracked: CompanyRecord) -> AsyncMock:
    store = AsyncMock()
    store.list_companies.return_value = list(tracked)
    return store


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = portfolio_web()

    def factory(timeout: float = 30.0) -> httpx.AsyncClient:  # noqa: ARG001
        return httpx.AsyncClient(transport=httpx.MockTransport(fake))

    monkeypatch.setattr(discover_module, "create_http_client", factory)
    return fake


@pytest.mark.usefixtures("served")
def test_dry_run_prints_origin_counts_and_plan_and_writes_no_company(
    tmp_path: Path,
) -> None:
    store = fake_store(CompanyRecord(slug="tracked", ats_vendor="greenhouse"))

    result = CliRunner().invoke(discover_companies, [], obj=cli_app(tmp_path, store))

    assert result.exit_code == 0, result.output
    assert (
        "getro:jobs.vc.example: 4 listed, 3 looked up, 0 cached, 0 deferred, "
        "0 lookup errors, 2 on an ATS, 1 new, 1 already tracked"
    ) in result.output
    assert "a16z: 1 listed, 1 looked up" in result.output
    assert "hn: 2 listed, 0 looked up" in result.output
    for line in (
        "would add acme (ashby)",
        "would add gone (lever)",
        "would add harvey (ashby)",
        "would add hnco (lever)",
    ):
        assert line in result.output
    assert "Dry-run: 4 new, 1 already tracked." in result.output
    assert "Lookup cache: 4 entries" in result.output
    store.upsert_company.assert_not_awaited()
    assert (tmp_path / CACHE_FILE_NAME).exists()


def test_apply_writes_marked_company_rows(tmp_path: Path, served: FakeWeb) -> None:
    store = fake_store(CompanyRecord(slug="gone", ats_vendor="lever"))

    result = CliRunner().invoke(
        discover_companies, ["--apply"], obj=cli_app(tmp_path, store)
    )

    assert result.exit_code == 0, result.output
    written = {
        call.args[0].slug: call.args[0] for call in store.upsert_company.await_args_list
    }
    assert set(written) == {"acme", "harvey", "hnco", "tracked"}
    assert {c.notes for c in written.values()} == {ATS_DISCOVERY_NOTE}
    vendor_apis = (ASHBY, GREENHOUSE, LEVER)
    assert not [r for r in served.requests if str(r.url).startswith(vendor_apis)]


@pytest.mark.usefixtures("served")
def test_max_lookups_bounds_the_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        discover_companies,
        ["--max-lookups", "1"],
        obj=cli_app(tmp_path, fake_store()),
    )

    assert result.exit_code == 0, result.output
    assert "1 looked up, 0 cached, 2 deferred" in result.output
    assert "a16z: 1 listed, 0 looked up, 0 cached, 1 deferred" in result.output


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_discovery_is_off_by_default_with_bounded_defaults() -> None:
    cfg = SourcesATSDiscoveryConfig()

    assert cfg.enabled is False
    assert (cfg.max_lookups_per_scan, cfg.max_concurrent, cfg.recheck_days) == (
        400,
        4,
        30,
    )
    assert (cfg.hn_threads, cfg.pacing_s) == (3, 0.5)


def test_getro_boards_are_normalized_and_deduped() -> None:
    cfg = SourcesATSDiscoveryConfig(
        getro_boards=[" https://jobs.accel.com/ ", "https://jobs.accel.com"]
    )

    assert cfg.getro_boards == ["https://jobs.accel.com"]


@pytest.mark.parametrize(
    "board",
    [
        "http://jobs.accel.com",
        "https://jobs.accel.com/jobs",
        "https://jobs.accel.com?q=x",
        "jobs.accel.com",
    ],
)
def test_getro_board_must_be_an_https_base_url(board: str) -> None:
    with pytest.raises(ValidationError, match="Getro board base URLs"):
        SourcesATSDiscoveryConfig(getro_boards=[board])


@pytest.mark.parametrize(
    "fields",
    [
        {"enabled": True, "a16z": False, "hn_threads": 0},
        {"max_lookups_per_scan": 0},
        {"max_concurrent": 0},
        {"pacing_s": -1},
        {"recheck_days": 0},
        {"hn_threads": 13},
        {"unknown": 1},
    ],
)
def test_discovery_rejects_invalid_settings(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SourcesATSDiscoveryConfig.model_validate(fields)


def test_discovery_requires_the_ats_source() -> None:
    with pytest.raises(ValidationError, match="needs the ATS source"):
        SourcesConfig.model_validate(
            {"ats": {"enabled": False}, "ats_discovery": {"enabled": True}}
        )
