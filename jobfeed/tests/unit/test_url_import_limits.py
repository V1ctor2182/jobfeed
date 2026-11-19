"""Whole official ATS boards have a bounded allowance separate from one page."""

import httpx
import pytest

from jobfeed.adapters.url_import import (
    MAX_BOARD_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    ImportFetchError,
    ImportHTTPClient,
    JobUrlImportService,
)

BOARD_URLS = [
    "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true",
    "https://api.lever.co/v0/postings/acme?mode=json",
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
]


async def public_fixture(_url):
    return True


@pytest.mark.parametrize("url", BOARD_URLS)
@pytest.mark.parametrize("size", [MAX_RESPONSE_BYTES + 1, MAX_BOARD_RESPONSE_BYTES])
async def test_official_whole_board_can_exceed_individual_page_limit(url, size):
    payload = b" " * size
    async with ImportHTTPClient(
        url_guard=public_fixture,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=payload)
        ),
    ) as client:
        response = await client.get(url)
    assert response.content == payload


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.test/jobs/1",
        "https://jobs.ashbyhq.com/acme/posting-id",
        "https://api.ashbyhq.com/posting-api/job-board/acme/posting-id?includeCompensation=true",
        "https://api.lever.co/v0/postings/acme/posting-id?mode=json",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?content=true",
        "https://api.ashbyhq.com.attacker.example/posting-api/job-board/acme?includeCompensation=true",
        "https://api.ashbyhq.com:8443/posting-api/job-board/acme?includeCompensation=true",
        "http://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true",
        "https://api.lever.co/v0/postings/acme?mode=json&unexpected=true",
        "https://api.lever.co/v0/postings/acme?mode=json&mode=json",
    ],
)
async def test_pages_individual_postings_and_nonexact_routes_keep_two_mb_limit(url):
    async with ImportHTTPClient(
        url_guard=public_fixture,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"x" * (MAX_RESPONSE_BYTES + 1)
            )
        ),
    ) as client:
        with pytest.raises(ImportFetchError, match="Posting response exceeds 2 MB"):
            await client.get(url)


class LargeBoardStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(17):
            yield b"x" * 1_000_000


async def test_board_allowance_still_enforces_sixteen_mb_stream_limit():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=LargeBoardStream())
    )
    async with ImportHTTPClient(
        url_guard=public_fixture, transport=transport
    ) as client:
        with pytest.raises(ImportFetchError, match="ATS board response exceeds 16 MB"):
            await client.get(BOARD_URLS[0])


async def test_final_redirect_destination_does_not_inherit_board_allowance():
    seen = []

    async def guard(url):
        seen.append(url)
        return True

    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(
                302, headers={"location": "https://careers.example.test/page"}
            )
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    async with ImportHTTPClient(
        url_guard=guard, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ImportFetchError, match="Posting response exceeds 2 MB"):
            await client.get(BOARD_URLS[1])
    assert seen == [BOARD_URLS[1], "https://careers.example.test/page"]


async def test_large_whole_board_still_selects_only_exact_requested_posting(tmp_path):
    wanted = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"

    def posting(identifier, title):
        return {
            "id": identifier,
            "title": title,
            "jobUrl": f"https://jobs.ashbyhq.com/acme/{identifier}",
            "descriptionPlain": "A real platform engineering role.",
        }

    body = {
        "jobs": [posting(other, "Other role"), posting(wanted, "Requested role")],
        "boardMetadata": "x" * MAX_RESPONSE_BYTES,
    }
    async with ImportHTTPClient(
        url_guard=public_fixture,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    ) as client:
        result = await JobUrlImportService(tmp_path / "unused.sqlite")._fetch(
            f"https://jobs.ashbyhq.com/acme/{wanted}", client
        )
    assert result.title == "Requested role"
    assert result.canonical_id == wanted
    assert result.url == f"https://jobs.ashbyhq.com/acme/{wanted}"
