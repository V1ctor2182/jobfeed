"""Speedrun source behavior against a fake API on httpx.MockTransport."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import click
import httpx
import pytest

from jobfeed.adapters.sources import speedrun
from jobfeed.adapters.sources._http import ATSFetchError
from jobfeed.adapters.sources.speedrun import SpeedrunSource, map_speedrun_job
from jobfeed.cli._scan_sources import build_scan_sources
from jobfeed.cli.scan import SOURCE_CHOICES
from jobfeed.config import Settings, SourcesSpeedrunConfig
from jobfeed.domain.models import QualityBand
from jobfeed.ports.source import (
    PartialSourceFetchError,
    SourceFetchProgress,
    StoredEnrichment,
)

SEARCH_URL = (
    "https://speedrun-talent-network.com/api/v1/jobs"
    "?fn=engineering&lvl=new_grad,junior,mid"
)
OTHER_SEARCH_URL = "https://speedrun-talent-network.com/api/v1/jobs?fn=product"
PACING_S = 0.25
RETRY_AFTER_S = 5.0
HTTP_TOO_MANY_REQUESTS = 429
FULL_JD = "Build reliable things. " * 100
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/5284428007?gh_jid=5284428007"

Reply = httpx.Response | dict[str, Any] | Exception


def _row(job_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": f"Engineer {job_id}",
        "company": "Acme",
        "url": f"https://speedrun-talent-network.com/jobs/{job_id}?utm_source=jobfeed",
        "location": "New York, NY",
        "remote": False,
        "published_at": "2026-11-05T18:03:51.259+00:00",
        "stealth": False,
        **overrides,
    }


def _detail(job_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "status": "open",
        "url": f"https://speedrun-talent-network.com/jobs/{job_id}",
        "title": f"  Engineer {job_id}  ",
        "company": "Acme",
        "stealth": False,
        "location": "New York, NY",
        "remote": False,
        "published_at": "2026-11-05T18:03:51.259+00:00",
        "apply": {"kind": "external", "url": GREENHOUSE_URL},
        "description_text": FULL_JD,
        **overrides,
    }


def _rate_limit(**headers: str) -> httpx.Response:
    return httpx.Response(
        429,
        headers={key.replace("_", "-"): value for key, value in headers.items()},
        json={"error": {"code": "rate_limited", "message": "slow down"}},
    )


def _job_reply(job_id: str, **overrides: Any) -> dict[str, Any]:
    return {"job": _detail(job_id, **overrides), "source": "jobfeed"}


def _list_page(rows: list[dict[str, Any]], total_pages: int) -> dict[str, Any]:
    return {"jobs": rows, "total_pages": total_pages, "source": "jobfeed"}


class FakeApi:
    """Serves list pages per ``fn`` filter and scripted replies per job id.

    A job with no scripted replies answers with an open detail object.
    """

    def __init__(
        self,
        pages: dict[str, list[list[dict[str, Any]]]] | list[list[dict[str, Any]]],
        details: dict[str, list[Reply]] | None = None,
        list_replies: list[Reply] | None = None,
    ) -> None:
        self.pages = pages if isinstance(pages, dict) else {"engineering": pages}
        self.details = {key: deque(value) for key, value in (details or {}).items()}
        self.list_replies = deque(list_replies or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/jobs":
            return self._list(request)
        job_id = request.url.path.rsplit("/", 1)[-1]
        replies = self.details.get(job_id)
        return self._resolve(
            replies.popleft() if replies else _job_reply(job_id), request
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        if self.list_replies:
            return self._resolve(self.list_replies.popleft(), request)
        pages = self.pages[request.url.params["fn"]]
        page = int(request.url.params["page"])
        return httpx.Response(200, json=_list_page(pages[page], len(pages)))

    @staticmethod
    def _resolve(reply: Reply, request: httpx.Request) -> httpx.Response:
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply, request=request)

    def list_pages_requested(self) -> list[int]:
        return [
            int(r.url.params["page"])
            for r in self.requests
            if r.url.path == "/api/v1/jobs"
        ]

    def detail_ids_requested(self) -> list[str]:
        return [
            r.url.path.rsplit("/", 1)[-1]
            for r in self.requests
            if r.url.path != "/api/v1/jobs"
        ]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(speedrun.asyncio, "sleep", fake_sleep)
    return waited


def _source(
    api: Callable[[httpx.Request], httpx.Response],
    *,
    search_urls: list[str] | None = None,
    lookup: Any = None,
    **config: Any,
) -> SpeedrunSource:
    return SpeedrunSource(
        client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        config=SourcesSpeedrunConfig(
            enabled=True,
            search_urls=search_urls or [SEARCH_URL],
            pacing_s=PACING_S,
            **config,
        ),
        logger=MagicMock(),
        enrichment_lookup=lookup,
    )


def _lookup(stored: dict[str, StoredEnrichment | None]) -> SimpleNamespace:
    async def get_enrichment(
        *, platform: str, canonical_id: str
    ) -> StoredEnrichment | None:
        assert platform == "speedrun"
        return stored.get(canonical_id)

    return SimpleNamespace(get_enrichment=get_enrichment)


def _ids(postings: list[Any]) -> list[str]:
    return [posting.canonical_id for posting in postings]


# --- paging ---------------------------------------------------------------


async def test_paging_stops_at_total_pages_and_requests_carry_filters():
    api = FakeApi([[_row("a")], [_row("b")]])

    postings = await _source(api).fetch_jobs({})

    assert api.list_pages_requested() == [0, 1]
    assert _ids(postings) == ["a", "b"]
    for request in api.requests:
        assert request.url.params["source"] == "jobfeed"
    for request in api.requests[:2]:
        assert request.url.params["fn"] == "engineering"
        assert request.url.params["lvl"] == "new_grad,junior,mid"


async def test_search_url_source_and_page_values_are_overridden():
    api = FakeApi([[_row("a")]])
    url = f"{SEARCH_URL}&source=other&page=7"

    await _source(api, search_urls=[url]).fetch_jobs({})

    first = api.requests[0]
    assert first.url.params.get_list("source") == ["jobfeed"]
    assert first.url.params.get_list("page") == ["0"]


async def test_paging_stops_once_max_jobs_collected():
    api = FakeApi(
        [
            [_row("a"), _row("b")],
            [_row("c"), _row("d")],
            [_row("e"), _row("f")],
        ]
    )

    postings = await _source(api, max_jobs=3).fetch_jobs({})

    assert api.list_pages_requested() == [0, 1]
    assert _ids(postings) == ["a", "b", "c"]
    assert api.detail_ids_requested() == ["a", "b", "c"]


async def test_ids_merge_across_search_urls_in_order():
    api = FakeApi(
        {
            "engineering": [[_row("a"), _row("shared")]],
            "product": [[_row("shared"), _row("z")]],
        }
    )

    postings = await _source(
        api, search_urls=[SEARCH_URL, OTHER_SEARCH_URL]
    ).fetch_jobs({})

    assert _ids(postings) == ["a", "shared", "z"]
    assert api.detail_ids_requested() == ["a", "shared", "z"]


async def test_stealth_rows_are_dropped_and_do_not_use_the_job_budget():
    api = FakeApi([[_row("masked", stealth=True), _row("a"), _row("b")]])

    postings = await _source(api, max_jobs=2).fetch_jobs({})

    assert _ids(postings) == ["a", "b"]
    assert "masked" not in api.detail_ids_requested()


# --- pacing ---------------------------------------------------------------


async def test_every_request_after_the_first_is_paced(sleeps):
    api = FakeApi([[_row("a"), _row("b")]])

    await _source(api).fetch_jobs({})

    assert api.list_pages_requested() == [0]
    assert api.detail_ids_requested() == ["a", "b"]
    assert sleeps == [PACING_S, PACING_S]


async def test_reused_stored_jd_costs_no_request_and_no_sleep(sleeps):
    stored = StoredEnrichment(
        jd_text=FULL_JD, quality=QualityBand.FULL, enriched_at=NOW
    )
    api = FakeApi([[_row("a"), _row("b")]])

    await _source(api, lookup=_lookup({"a": stored, "b": stored})).fetch_jobs({})

    assert api.list_pages_requested() == [0]
    assert api.detail_ids_requested() == []
    assert sleeps == []


# --- mapping --------------------------------------------------------------


def test_mapping_detail_to_posting():
    posting = map_speedrun_job(
        _detail(
            "job-1",
            published_at="2026-11-04T09:28:41-05:00",
            url="https://speedrun-talent-network.com/jobs/job-1",
        ),
        discovered_at=NOW,
    )

    assert posting.platform == "speedrun"
    assert posting.canonical_id == "job-1"
    assert posting.url == "https://speedrun-talent-network.com/jobs/job-1"
    assert posting.apply_url == GREENHOUSE_URL
    assert posting.identity_evidence_url == GREENHOUSE_URL
    assert posting.external_identity == "greenhouse:5284428007"
    assert posting.title == "Engineer job-1"
    assert posting.company == "Acme"
    assert posting.posted_at == datetime(2026, 11, 4, 14, 28, 41, tzinfo=UTC)
    assert posting.posted_at.utcoffset().total_seconds() == 0
    assert posting.jd_text == FULL_JD.strip()
    assert posting.jd_quality == QualityBand.FULL
    assert posting.enriched_at == NOW
    assert posting.enrich_source == "speedrun_api"


@pytest.mark.parametrize(
    "apply",
    [None, {"kind": "onsite", "url": "https://speedrun-talent-network.com/x"}],
)
def test_apply_url_falls_back_to_job_url(apply):
    posting = map_speedrun_job(_detail("job-1", apply=apply), discovered_at=NOW)

    assert posting.apply_url == "https://speedrun-talent-network.com/jobs/job-1"
    assert posting.identity_evidence_url == posting.apply_url
    assert posting.external_identity is None


@pytest.mark.parametrize(
    ("location", "remote", "expected"),
    [
        ("Austin, TX", True, "Austin, TX"),
        (None, True, "Remote"),
        (None, False, "Unknown"),
    ],
)
def test_location_fallbacks(location, remote, expected):
    posting = map_speedrun_job(
        _detail("job-1", location=location, remote=remote), discovered_at=NOW
    )

    assert posting.location == expected


@pytest.mark.parametrize(
    ("description", "quality", "enriched_at"),
    [
        (None, QualityBand.MISSING, None),
        ("x" * 100, QualityBand.STUB, None),
        ("x" * 300, QualityBand.PARTIAL, None),
        ("x" * 800, QualityBand.GOOD, NOW),
        ("x" * 1500, QualityBand.FULL, NOW),
    ],
)
def test_quality_bands_stamp_enriched_at_only_when_complete(
    description, quality, enriched_at
):
    posting = map_speedrun_job(
        _detail("job-1", description_text=description), discovered_at=NOW
    )

    assert posting.jd_quality == quality
    assert posting.enriched_at == enriched_at
    assert posting.jd_text == description


def test_null_published_at_maps_to_none():
    posting = map_speedrun_job(_detail("job-1", published_at=None), discovered_at=NOW)

    assert posting.posted_at is None


@pytest.mark.parametrize("field", ["id", "url", "title", "company"])
def test_missing_required_field_is_rejected(field):
    with pytest.raises(ValueError, match=field):
        map_speedrun_job(_detail("job-1", **{field: None}), discovered_at=NOW)


# --- stored JD reuse ------------------------------------------------------


@pytest.mark.parametrize("quality", [QualityBand.GOOD, QualityBand.FULL])
async def test_complete_stored_jd_skips_detail_request(quality):
    enriched_at = datetime(2026, 1, 1, tzinfo=UTC)
    stored = StoredEnrichment(
        jd_text="Stored complete description. " * 40,
        quality=quality,
        enriched_at=enriched_at,
        enrich_source="speedrun_api",
        apply_url=GREENHOUSE_URL,
        external_identity="greenhouse:5284428007",
    )
    api = FakeApi([[_row("a", title="  Renamed  ", location=None, remote=True)]])
    source = _source(api, lookup=_lookup({"a": stored}))

    postings = await source.fetch_jobs({})

    assert api.detail_ids_requested() == []
    posting = postings[0]
    assert posting.url == "https://speedrun-talent-network.com/jobs/a"
    assert posting.title == "Renamed"
    assert posting.location == "Remote"
    assert posting.jd_text == stored.jd_text
    assert posting.jd_quality == quality
    assert posting.enriched_at == enriched_at
    assert posting.enrich_source == "speedrun_api"
    assert posting.apply_url == GREENHOUSE_URL
    assert posting.external_identity == "greenhouse:5284428007"
    assert source.stats == {"reused": 1, "fetched": 0, "closed": 0, "failed": 0}


@pytest.mark.parametrize(
    "stored",
    [
        None,
        StoredEnrichment(jd_text="short", quality=QualityBand.STUB, enriched_at=None),
        StoredEnrichment(
            jd_text="partial", quality=QualityBand.PARTIAL, enriched_at=None
        ),
        StoredEnrichment(jd_text="   ", quality=QualityBand.FULL, enriched_at=None),
    ],
)
async def test_new_or_incomplete_stored_jd_fetches_detail(stored):
    api = FakeApi([[_row("a")]])
    source = _source(api, lookup=_lookup({"a": stored}))

    postings = await source.fetch_jobs({})

    assert api.detail_ids_requested() == ["a"]
    assert postings[0].jd_text == FULL_JD.strip()
    assert source.stats["fetched"] == 1


# --- closed and missing jobs ----------------------------------------------


async def test_closed_and_404_details_are_skipped():
    api = FakeApi(
        [[_row("open"), _row("closed"), _row("gone")]],
        details={
            "closed": [_job_reply("closed", status="closed")],
            "gone": [
                httpx.Response(
                    404, json={"error": {"code": "not_found", "message": "no job"}}
                )
            ],
        },
    )
    source = _source(api)

    postings = await source.fetch_jobs({})

    assert _ids(postings) == ["open"]
    assert source.stats == {"reused": 0, "fetched": 1, "closed": 2, "failed": 0}


# --- rate limit -----------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected_wait"),
    [
        ({"Retry_After": "7"}, 7.0),
        ({}, 60.0),
        ({"Retry_After": "Fri, 06 Nov 2026 12:00:00 GMT"}, 60.0),
        ({"Retry_After": "900"}, 120.0),
    ],
)
async def test_429_waits_then_retries_once(sleeps, headers, expected_wait):
    api = FakeApi(
        [[_row("a")]],
        details={"a": [_rate_limit(**headers), _job_reply("a")]},
    )

    postings = await _source(api).fetch_jobs({})

    assert _ids(postings) == ["a"]
    assert api.detail_ids_requested() == ["a", "a"]
    assert expected_wait in sleeps


async def test_second_429_stops_fetching_and_keeps_finished_postings():
    api = FakeApi(
        [[_row("done"), _row("limited"), _row("never")]],
        details={"limited": [_rate_limit(), _rate_limit()]},
    )
    source = _source(api)

    with pytest.raises(PartialSourceFetchError) as caught:
        await source.fetch_jobs({})

    assert caught.value.warning is True
    assert _ids(caught.value.postings) == ["done"]
    assert api.detail_ids_requested() == ["done", "limited", "limited"]
    assert source.stats["failed"] == 1


async def test_429_on_a_list_page_retries_once(sleeps):
    api = FakeApi(
        [[_row("a")]], list_replies=[_rate_limit(Retry_After=str(int(RETRY_AFTER_S)))]
    )

    postings = await _source(api).fetch_jobs({})

    assert _ids(postings) == ["a"]
    assert RETRY_AFTER_S in sleeps


async def test_second_429_on_later_list_page_stops_all_requests():
    api = FakeApi(
        [],
        list_replies=[_list_page([_row("a")], 2), _rate_limit(), _rate_limit()],
    )

    with pytest.raises(PartialSourceFetchError) as caught:
        await _source(api).fetch_jobs({})

    assert caught.value.postings == []
    assert caught.value.warning is True
    assert api.detail_ids_requested() == []


# --- failure contract -----------------------------------------------------


async def test_detail_500_continues_and_raises_partial_with_the_rest():
    api = FakeApi(
        [[_row("a"), _row("broken"), _row("c")]],
        details={"broken": [httpx.Response(500, json={"error": {"code": "boom"}})]},
    )
    source = _source(api)

    with pytest.raises(PartialSourceFetchError) as caught:
        await source.fetch_jobs({})

    assert caught.value.warning is True
    assert _ids(caught.value.postings) == ["a", "c"]
    assert source.stats == {"reused": 0, "fetched": 2, "closed": 0, "failed": 1}


async def test_detail_transport_error_continues_and_raises_partial():
    api = FakeApi(
        [[_row("a"), _row("flaky"), _row("c")]],
        details={"flaky": [httpx.ReadTimeout("slow")]},
    )

    with pytest.raises(PartialSourceFetchError) as caught:
        await _source(api).fetch_jobs({})

    assert _ids(caught.value.postings) == ["a", "c"]


@pytest.mark.parametrize(
    "bad_reply",
    [
        {"source": "jobfeed"},
        _job_reply("bad", title=None),
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
async def test_malformed_detail_counts_as_failed_and_continues(bad_reply):
    api = FakeApi([[_row("a"), _row("bad")]], details={"bad": [bad_reply]})
    source = _source(api)

    with pytest.raises(PartialSourceFetchError) as caught:
        await source.fetch_jobs({})

    assert _ids(caught.value.postings) == ["a"]
    assert source.stats["failed"] == 1


@pytest.mark.parametrize(
    ("reply", "status_code"),
    [
        (httpx.Response(500, json={"error": {"code": "boom"}}), 500),
        (httpx.ConnectError("refused"), None),
        (httpx.Response(200, json={"jobs": "nope"}), None),
    ],
)
async def test_list_failure_with_nothing_collected_raises_fetch_error(
    reply, status_code
):
    api = FakeApi([], list_replies=[reply])

    with pytest.raises(ATSFetchError) as caught:
        await _source(api).fetch_jobs({})

    assert caught.value.vendor == "speedrun"
    assert caught.value.status_code == status_code
    assert api.detail_ids_requested() == []


async def test_list_second_429_with_nothing_collected_raises_fetch_error():
    api = FakeApi([], list_replies=[_rate_limit(), _rate_limit()])

    with pytest.raises(ATSFetchError) as caught:
        await _source(api).fetch_jobs({})

    assert caught.value.status_code == HTTP_TOO_MANY_REQUESTS


async def test_later_list_failure_keeps_collected_rows():
    api = FakeApi(
        [],
        list_replies=[
            _list_page([_row("a")], 1),
            httpx.Response(503, json={"error": {"code": "down"}}),
        ],
    )
    source = _source(api, search_urls=[SEARCH_URL, OTHER_SEARCH_URL])

    with pytest.raises(PartialSourceFetchError) as caught:
        await source.fetch_jobs({})

    assert caught.value.warning is True
    assert _ids(caught.value.postings) == ["a"]


async def test_store_lookup_failure_propagates_without_detail_requests():
    api = FakeApi([[_row("a")]])
    lookup = SimpleNamespace(
        get_enrichment=AsyncMock(side_effect=RuntimeError("DB unavailable"))
    )

    with pytest.raises(RuntimeError, match="DB unavailable"):
        await _source(api, lookup=lookup).fetch_jobs({})

    assert api.detail_ids_requested() == []


# --- progress and stats ---------------------------------------------------


async def test_progress_reports_listing_then_details():
    api = FakeApi([[_row("a"), _row("b")]])
    updates: list[SourceFetchProgress] = []

    await _source(api).fetch_jobs_with_progress({}, updates.append)

    assert [(u.phase, u.processed, u.total) for u in updates] == [
        ("fetching", 2, None),
        ("details", 0, 2),
        ("details", 1, 2),
        ("details", 2, 2),
    ]


# --- wiring ---------------------------------------------------------------


def _app(*, enabled: bool) -> dict[str, Any]:
    speedrun_config: dict[str, Any] = {"enabled": enabled}
    if enabled:
        speedrun_config["search_urls"] = [SEARCH_URL]
    return {
        "settings": Settings(
            sources={"ats": {"enabled": False}, "speedrun": speedrun_config}
        ),
        "store": MagicMock(),
        "logger": MagicMock(),
        "jobright_bridge": MagicMock(),
    }


async def test_all_builds_enabled_speedrun():
    async with AsyncExitStack() as stack:
        sources = await build_scan_sources(_app(enabled=True), "all", stack)

    assert [name for name, _source_obj, _config in sources] == ["speedrun"]
    assert isinstance(sources[0][1], SpeedrunSource)


async def test_all_skips_disabled_speedrun():
    app = _app(enabled=False)

    async with AsyncExitStack() as stack:
        sources = await build_scan_sources(app, "all", stack)

    assert sources == []
    app["logger"].info.assert_any_call(
        "scan_source_skipped", source="speedrun", reason="disabled"
    )


async def test_explicit_disabled_speedrun_is_a_click_error():
    async with AsyncExitStack() as stack:
        with pytest.raises(click.ClickException, match="speedrun"):
            await build_scan_sources(_app(enabled=False), "speedrun", stack)


def test_speedrun_is_a_cli_source_choice():
    assert "speedrun" in SOURCE_CHOICES
