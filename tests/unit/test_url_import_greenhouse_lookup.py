"""Company pages carrying gh_jid resolve to the exact Greenhouse posting."""

import json
import sqlite3

import httpx

from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.adapters.url_import import (
    ImportHTTPClient,
    JobUrlImportService,
    _greenhouse_page_tokens,
    _greenhouse_url_tokens,
)

API = "boards-api.greenhouse.io"


async def public_fixture(_url):
    return True


def greenhouse_job(job_id, *, company="Acme", absolute_url=None):
    return {
        "id": job_id,
        "title": "Platform Engineer",
        "absolute_url": absolute_url
        or f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "&lt;p&gt;Build and run the platform.&lt;/p&gt;",
        "company_name": company,
        "location": {"name": "New York"},
    }


def board_of(request):
    if request.url.host != API:
        return None
    return request.url.path.split("/")[3]


def client_for(handler, requested):
    def recording(request):
        requested.append(str(request.url))
        return handler(request)

    return ImportHTTPClient(
        url_guard=public_fixture, transport=httpx.MockTransport(recording)
    )


async def test_url_token_board_imports_without_requesting_company_page(tmp_path):
    url = "https://seatgeek.com/jobs/8227548?gh_jid=8227548"
    db = tmp_path / "jobfeed.sqlite"
    store = SQLiteStore(db)
    await store.connect()
    await store.close()
    requested = []

    def handler(request):
        if board_of(request) == "seatgeek":
            return httpx.Response(
                200, json=greenhouse_job(8227548, company="SeatGeek", absolute_url=url)
            )
        return httpx.Response(404)

    async with client_for(handler, requested) as client:
        result = await JobUrlImportService(db, client=client).import_url(url)

    assert result.status == "imported"
    assert not any("seatgeek.com" in seen for seen in requested)
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT platform, canonical_id, company, title FROM jobs WHERE id=?",
            (result.source_job_id,),
        ).fetchone()
    assert row == ("greenhouse", "8227548", "SeatGeek", "Platform Engineer")


async def test_page_embed_board_resolves_after_url_tokens_miss(tmp_path):
    url = "https://www.example-corp.com/careers/role?gh_jid=555"
    page = (
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeboard">'
        "</script>"
    )
    requested = []

    def handler(request):
        if request.url.host == "www.example-corp.com":
            return httpx.Response(200, text=page)
        if board_of(request) == "acmeboard":
            return httpx.Response(200, json=greenhouse_job(555))
        return httpx.Response(404)

    async with client_for(handler, requested) as client:
        posting = await JobUrlImportService(tmp_path / "unused.sqlite")._fetch(
            url, client
        )

    assert posting.platform == "greenhouse"
    assert posting.canonical_id == "555"
    assert posting.company == "Acme"
    assert requested[-1].startswith(f"https://{API}/v1/boards/acmeboard/jobs/555")


def json_ld_page(url):
    record = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "url": url,
        "title": "Data Analyst",
        "description": "<p>Analyze the data.</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
    }
    return f'<script type="application/ld+json">{json.dumps(record)}</script>'


async def test_board_returning_a_different_job_falls_back_to_page(tmp_path):
    url = "https://examplecorp.com/jobs/opening?gh_jid=555"
    requested = []

    def handler(request):
        if request.url.host == "examplecorp.com":
            return httpx.Response(200, text=json_ld_page(url))
        if board_of(request) == "examplecorp":
            return httpx.Response(200, json=greenhouse_job(999))
        return httpx.Response(404)

    async with client_for(handler, requested) as client:
        posting = await JobUrlImportService(tmp_path / "unused.sqlite")._fetch(
            url, client
        )

    assert posting.enrich_source == "json-ld"
    assert posting.title == "Data Analyst"
    assert any("/v1/boards/examplecorp/jobs/555" in seen for seen in requested)


async def test_no_board_match_imports_page_json_ld_as_before(tmp_path):
    url = "https://examplecorp.com/jobs/opening?gh_jid=555"
    requested = []

    def handler(request):
        if request.url.host == "examplecorp.com":
            return httpx.Response(200, text=json_ld_page(url))
        return httpx.Response(404)

    async with client_for(handler, requested) as client:
        posting = await JobUrlImportService(tmp_path / "unused.sqlite")._fetch(
            url, client
        )

    assert posting.platform == "url_import"
    assert posting.company == "Example Corp"
    assert posting.title == "Data Analyst"


async def test_no_board_match_and_missing_page_is_closed(tmp_path):
    db = tmp_path / "jobfeed.sqlite"
    store = SQLiteStore(db)
    await store.connect()
    await store.close()

    async with client_for(lambda _request: httpx.Response(404), []) as client:
        result = await JobUrlImportService(db, client=client).import_url(
            "https://examplecorp.com/jobs/opening?gh_jid=555"
        )

    assert result.status == "closed"


def test_url_tokens_strip_generic_labels_and_segments():
    assert _greenhouse_url_tokens(
        "https://www.pinterestcareers.com/jobs/?gh_jid=8227344"
    ) == ("pinterest",)
    assert "dominodatalab" in _greenhouse_url_tokens(
        "https://app.careerpuck.com/job-board/domino-data-lab/job/7992556?gh_jid=7992556"
    )
    assert _greenhouse_url_tokens(
        "https://seatgeek.com/jobs/8227548?gh_jid=8227548"
    ) == ("seatgeek",)


def test_url_tokens_are_capped_at_six():
    tokens = _greenhouse_url_tokens(
        "https://alpha.beta.gamma.com/delta/epsilon/zeta/eta/theta?gh_jid=1"
    )
    assert tokens == ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")


def test_page_tokens_find_each_embed_form_once():
    html = (
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=Acme">'
        '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">'
        '<iframe src="https://boards.greenhouse.io/embed/job_app?for=beta&token=1">'
        'fetch("https://boards-api.greenhouse.io/v1/boards/gamma/jobs")'
        '<a href="https://example.com/embed/job_board?for=other">'
    )
    assert _greenhouse_page_tokens(html) == ("acme", "beta", "gamma")
