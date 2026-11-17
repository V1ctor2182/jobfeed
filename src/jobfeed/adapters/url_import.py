"""Import exact posting URLs through guarded HTTP and coordinated SQLite writes."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from jobfeed.adapters.sources import _ats_ashby, _ats_greenhouse, _ats_lever
from jobfeed.adapters.sources._http import ATSFetchError, html_to_text
from jobfeed.adapters.sources.application_routes import URLGuard, public_application_url
from jobfeed.adapters.store.sqlite import SQLiteStore
from jobfeed.adapters.store.url_import_lock import hold_url_import
from jobfeed.domain.external_identity import external_identity, observed_identifier
from jobfeed.domain.models import JobPosting
from jobfeed.domain.posting_url import PostingURLKey, posting_url
from jobfeed.domain.quality import assess_quality

MAX_RESPONSE_BYTES = 2_000_000
MAX_BOARD_RESPONSE_BYTES = 16_000_000
ASCII_SPACE = 32
_BOARD_ENDPOINTS = (
    (
        "api.ashbyhq.com",
        r"/posting-api/job-board/[A-Za-z0-9_.-]+",
        [("includeCompensation", "true")],
    ),
    ("api.lever.co", r"/v0/postings/[A-Za-z0-9_.-]+", [("mode", "json")]),
    (
        "boards-api.greenhouse.io",
        r"/v1/boards/[A-Za-z0-9_.-]+/jobs",
        [("content", "true")],
    ),
)
GREENHOUSE_BOARD_ATTEMPTS = 6
_GENERIC_HOST_LABELS = frozenset(
    {"www", "careers", "career", "jobs", "job", "apply", "app", "boards", "hr"}
    | {"com", "io", "co", "org", "net", "ai", "us"}
)
_GENERIC_PATH_SEGMENTS = frozenset(
    {"careers", "jobs", "job", "job-board", "openings", "apply", "hr", "en", "us"}
)
_GREENHOUSE_PAGE_TOKEN = re.compile(
    r"greenhouse\.io\\?/(?:embed\\?/job_board(?:\\?/js)?\?for=|"
    r"embed\\?/job_app\?for=|v1\\?/boards\\?/)([A-Za-z0-9_-]+)"
)


@dataclass(frozen=True)
class ImportResult:
    """An intake outcome with exact source and canonical IDs when available.

    Status is imported, existing, needs_details, closed, identity_review, or error.
    The source_job_id identifies the saved posting used for queue admission.
    The real_job_id identifies its canonical job. Reason explains a refusal or error.
    """

    status: Literal[
        "imported", "existing", "needs_details", "closed", "identity_review", "error"
    ]
    source_job_id: int | None = None
    real_job_id: int | None = None
    reason: str | None = None


class ImportFetchError(ValueError):
    """A posting fetch or structured record failed an intake safety requirement."""


def observed_linkedin_posting(payload: object) -> JobPosting:
    """Validate one native browser observation without fetching or inferring a route.

    Args:
        payload: Observed LinkedIn posting sent by the browser.

    Returns:
        The posting built from the observed fields.

    Raises:
        ValueError: If the payload is not an object, a field is missing or too
            long, the URL does not name the same native LinkedIn job, the
            timestamp lacks a zone or is in the future, or the Easy Apply label
            does not match the observed company and title.
    """
    if not isinstance(payload, dict):
        raise ValueError("Observed posting must be a JSON object")
    fields = {}
    for name, limit in (
        ("url", 8192),
        ("native_job_id", 30),
        ("company", 500),
        ("title", 500),
        ("location", 1000),
        ("jd_text", 200000),
        ("observed_at", 100),
        ("browser_id", 200),
        ("tab_id", 200),
        ("easy_apply_label", 1500),
        ("page_snapshot", 500000),
    ):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Observed posting requires a bounded nonempty {name}")
        fields[name] = value.strip()
    url = fields["url"]
    key = PostingURLKey.parse(url)
    parsed = urlsplit(url)
    native_id = fields["native_job_id"]
    if (
        any(character.isspace() or ord(character) < ASCII_SPACE for character in url)
        or parsed.scheme != "https"
        or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
        or parsed.port not in {None, 443}
        or not re.fullmatch(r"[1-9][0-9]*", native_id)
        or not re.fullmatch(rf"/jobs/view/{re.escape(native_id)}/?", parsed.path)
        or key.native_identity != f"linkedin:{native_id}"
    ):
        raise ValueError("Observed URL must identify the same native LinkedIn job ID")
    observed_at = datetime.fromisoformat(fields["observed_at"].replace("Z", "+00:00"))
    if observed_at.tzinfo is None:
        raise ValueError("Observed timestamp requires a timezone")
    if observed_at > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("Observed timestamp cannot be in the future")
    label = fields["easy_apply_label"]
    snapshot = fields["page_snapshot"]
    if (
        label != f"Easy Apply to {fields['title']} at {fields['company']}"
        or label not in snapshot
    ):
        raise ValueError(
            "Native Easy Apply label must match the observed company and title"
        )
    _validate_observed_detail_identity(snapshot, fields["title"], native_id, label)
    return JobPosting(
        platform="linkedin",
        canonical_id=native_id,
        url=f"https://www.linkedin.com/jobs/view/{native_id}",
        title=fields["title"],
        company=fields["company"],
        location=fields["location"],
        jd_text=fields["jd_text"],
        jd_quality=assess_quality(fields["jd_text"]),
        discovered_at=observed_at,
        enriched_at=observed_at,
        enrich_source="browser-observed:linkedin",
        identity_evidence_url=url,
    )


def _validate_observed_detail_identity(
    snapshot: str, title: str, native_id: str, label: str
) -> None:
    title_pattern = re.escape(title)
    headings = list(
        re.finditer(
            rf'^([ ]*)- heading "{title_pattern}" \[level=1\]:\n'
            rf'\1  - link "{title_pattern}":\n\1    - /url: (\S+)',
            snapshot,
            re.MULTILINE,
        )
    )
    if len(headings) != 1:
        raise ValueError(
            "Snapshot requires one matching native job detail heading link"
        )
    heading = headings[0]
    detail_url = urljoin("https://www.linkedin.com", heading[2])
    key = PostingURLKey.parse(detail_url)
    parsed = urlsplit(detail_url)
    detail_end = re.search(
        r"^ *- heading .*\[level=1\]", snapshot[heading.end() :], re.MULTILINE
    )
    detail = snapshot[heading.end() :]
    if detail_end is not None:
        detail = detail[: detail_end.start()]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
        or key.native_identity != f"linkedin:{native_id}"
        or not re.fullmatch(rf"/jobs/view/{re.escape(native_id)}/?", parsed.path)
        or f'- button "{label}":' not in detail
    ):
        raise ValueError(
            "Snapshot job detail identity does not match the observed posting"
        )


class ImportHTTPClient(httpx.AsyncClient):
    """Bound every GET, including requests made by the existing ATS adapters.

    Args:
        url_guard: Async check applied to each destination before requesting it.
        transport: Optional HTTP transport for deterministic fixture requests.
    """

    def __init__(
        self,
        *,
        url_guard: URLGuard = public_application_url,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            follow_redirects=False, timeout=10, trust_env=False, transport=transport
        )
        self.url_guard = url_guard

    async def get(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Fetch a bounded response after guarding every redirect destination.

        Args:
            url: Requested posting or ATS endpoint.
            **kwargs: Request options passed to the HTTP stream.

        Returns:
            The fully read response with its final request and decoded body.

        Raises:
            ImportFetchError: A destination, redirect, or body exceeds intake limits.
            httpx.HTTPError: The HTTP transport fails.
        """
        kwargs.pop("follow_redirects", None)
        target = str(url)
        for _ in range(6):
            if not await self.url_guard(target):
                raise ImportFetchError("Destination is not a public HTTP URL")
            async with self.stream(
                "GET", target, follow_redirects=False, **kwargs
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ImportFetchError("Redirect has no destination")
                    target = urljoin(target, location)
                    continue
                return await _read_bounded_response(response)
        raise ImportFetchError("Posting exceeded five redirects")


async def _read_bounded_response(response: httpx.Response) -> httpx.Response:
    limit = MAX_RESPONSE_BYTES
    parsed = urlsplit(str(response.request.url))
    if (
        parsed.scheme == "https"
        and parsed.port in {None, 443}
        and not parsed.username
        and not parsed.password
    ):
        for host, path, query in _BOARD_ENDPOINTS:
            if (
                parsed.hostname == host
                and re.fullmatch(path, parsed.path)
                and parse_qsl(parsed.query, keep_blank_values=True) == query
            ):
                limit = MAX_BOARD_RESPONSE_BYTES
                break
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ImportFetchError(
                "ATS board response exceeds 16 MB"
                if limit == MAX_BOARD_RESPONSE_BYTES
                else "Posting response exceeds 2 MB"
            )
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-encoding", "content-length"}
    }
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=bytes(chunks),
        request=response.request,
    )


class JobUrlImportService:
    """Resolve one posting URL and save its exact source under an import lock.

    A supplied client remains owned by the caller. Otherwise each import owns and
    closes its client. The import deadline includes waiting for coordination.

    Args:
        db_path: Initialized Jobfeed database used for lookup and persistence.
        client: Optional caller-owned guarded HTTP client.
    """

    def __init__(self, db_path: Path, *, client: ImportHTTPClient | None = None):
        self.db_path = db_path
        self.client = client

    def _existing(self, key: PostingURLKey) -> list[sqlite3.Row]:
        def normalized(value: str | None) -> str | None:
            if not value:
                return None
            try:
                return posting_url(value)
            except ValueError:
                return None

        select = (
            "SELECT id,real_job_id,platform,canonical_id,title,company,location,"
            "jd_text,jd_quality,closed_at,url,apply_url FROM jobs "
        )
        order = (
            " ORDER BY closed_at IS NOT NULL, "
            "(NULLIF(trim(title),'') IS NULL OR NULLIF(trim(company),'') IS NULL "
            "OR NULLIF(trim(jd_text),'') IS NULL OR NULLIF(trim(url),'') IS NULL), id"
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.create_function("posting_url", 1, normalized, deterministic=True)
            observed = observed_identifier(key.normalized_url)
            condition = "posting_url(url)=? OR posting_url(apply_url)=?"
            parameters: list[str | None] = [key.normalized_url, key.normalized_url]
            if observed is not None:
                condition += (
                    " OR external_identity=? OR id IN ("
                    "SELECT evidence_job_id FROM real_job_identifiers "
                    "WHERE provider=? AND scope=? AND native_id=?)"
                )
                parameters.extend(
                    (
                        key.native_identity,
                        observed.provider,
                        observed.scope,
                        observed.native_id,
                    )
                )
            return connection.execute(
                select + "WHERE " + condition + order, parameters
            ).fetchall()

    async def import_url(self, url: str) -> ImportResult:
        """Import or locate a posting without queueing an application.

        Args:
            url: One public HTTP or HTTPS posting URL.

        Returns:
            A source receipt or refusal. Malformed URLs and fetch validation
            failures return needs_details. Closed postings return closed,
            ambiguous identities return identity_review, and direct HTTP failures
            or deadline expiry return error. ATS fetch failures return needs_details,
            except HTTP 404 or 410 returns closed. Successful saves return imported or
            existing with the exact source ID and its canonical job ID.

        Raises:
            sqlite3.Error: Storage or coordination fails outside lock contention.
            OSError: Coordination files cannot be accessed.
            asyncio.CancelledError: The caller cancels the import. Locks and an
                internally owned client are released during cancellation.
        """
        try:
            key = PostingURLKey.parse(url)
        except ValueError:
            return ImportResult(
                "needs_details", reason="Supply a public HTTP or HTTPS posting URL"
            )
        try:
            async with asyncio.timeout(40), hold_url_import(self.db_path, key):
                if self.client is not None:
                    return await self._import(key, self.client)
                async with ImportHTTPClient() as client:
                    return await self._import(key, client)
        except ImportFetchError as exc:
            return ImportResult("needs_details", reason=str(exc))
        except ATSFetchError as exc:
            return ImportResult(
                "closed" if exc.status_code in {404, 410} else "needs_details",
                reason=f"ATS request failed ({exc.status_code or 'invalid response'})",
            )
        except (httpx.HTTPError, TimeoutError):
            return ImportResult(
                "error",
                reason="Import wait or fetch timed out or failed. Retry is safe.",
            )

    async def import_observed_posting(self, payload: object) -> ImportResult:
        """Save validated browser data through the same lock and source persistence.

        Args:
            payload: Observed LinkedIn posting sent by the browser.

        Returns:
            The import result; invalid observations and lock timeouts are reported
            in the result rather than raised.
        """
        try:
            posting = observed_linkedin_posting(payload)
        except (ValueError, TypeError) as exc:
            return ImportResult("needs_details", reason=str(exc))
        key = PostingURLKey.parse(posting.url)
        try:
            async with asyncio.timeout(40), hold_url_import(self.db_path, key):
                return await self._import(key, None, observed=posting)
        except TimeoutError:
            return ImportResult(
                "error", reason="Import wait timed out; save outcome is unknown"
            )

    async def _import(
        self,
        key: PostingURLKey,
        client: ImportHTTPClient | None,
        *,
        observed: JobPosting | None = None,
    ) -> ImportResult:
        url = key.normalized_url
        existing = self._existing(key)
        if len({row["real_job_id"] for row in existing}) > 1:
            return ImportResult(
                "identity_review", reason="URL matches more than one canonical job"
            )
        existing = [
            row
            for row in existing
            if not row["closed_at"]
            or external_identity(row["url"]) == external_identity(url)
        ]
        if existing:
            row = existing[0]
            if row["closed_at"]:
                return ImportResult(
                    "closed", row["id"], row["real_job_id"], "Stored posting is closed"
                )
            if _complete_source(row):
                return ImportResult("existing", row["id"], row["real_job_id"])
        if observed is None:
            assert client is not None
            posting = await self._fetch_public(url, client)
        else:
            posting = observed
        if posting.closed_at:
            return ImportResult("closed", reason="Posting has expired")
        if (
            not posting.title.strip()
            or not posting.company.strip()
            or not posting.jd_text
        ):
            raise ImportFetchError("Posting is missing title, company, or description")
        if existing:
            posting = replace(
                posting,
                platform=existing[0]["platform"],
                canonical_id=existing[0]["canonical_id"],
                title=(existing[0]["title"] or "").strip() or posting.title,
                company=(existing[0]["company"] or "").strip() or posting.company,
                location=(existing[0]["location"] or "").strip() or posting.location,
                apply_url=(existing[0]["apply_url"] or "").strip()
                or (posting.apply_url if observed is not None else posting.url),
                url=(existing[0]["url"] or "").strip() or posting.url,
                jd_text=(existing[0]["jd_text"] or "").strip() or posting.jd_text,
            )
        posting = replace(
            posting,
            url=posting_url(posting.url),
            apply_url=(
                None
                if observed is not None and not posting.apply_url
                else posting_url(posting.apply_url or posting.url)
            ),
            identity_evidence_url=url,
            enrich_source=f"url-import:{posting.enrich_source}",
            jd_quality=assess_quality(posting.jd_text),
        )
        store = SQLiteStore(self.db_path)
        await store.connect()
        try:
            saved = await store.save_job(posting)
        finally:
            await store.close()
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT real_job_id FROM jobs WHERE id=?", (saved.job_id,)
            ).fetchone()
        return ImportResult(
            "imported" if saved.inserted else "existing", int(saved.job_id), row[0]
        )

    async def _fetch_public(self, url: str, client: ImportHTTPClient) -> JobPosting:
        if not await client.url_guard(url):
            raise ImportFetchError("Supply a public HTTP or HTTPS posting URL")
        posting = await self._fetch(url, client)
        if not await client.url_guard(posting.url):
            raise ImportFetchError("Returned posting URL is not public")
        return posting

    async def _fetch(self, url: str, client: ImportHTTPClient) -> JobPosting:
        parsed = urlsplit(url)
        greenhouse_path = re.fullmatch(r"/([^/]+)/jobs/(\d+)/?", parsed.path)
        board_path = re.fullmatch(
            r"/([^/]+)/([^/]+)(?:/(?:apply|application))?/?", parsed.path
        )
        identity = external_identity(url)
        now = datetime.now(UTC)
        posting = None
        if (
            parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
            and greenhouse_path
        ):
            posting = await _ats_greenhouse.fetch_job(
                client, greenhouse_path[1], greenhouse_path[2], discovered_at=now
            )
        elif (
            parsed.hostname in {"jobs.ashbyhq.com", "jobs.lever.co"}
            and identity
            and board_path
        ):
            adapter = (
                _ats_ashby if parsed.hostname == "jobs.ashbyhq.com" else _ats_lever
            )
            jobs = await adapter.fetch_jobs(client, board_path[1], discovered_at=now)
            posting = next(
                (
                    job
                    for job in jobs
                    if job.canonical_id.lower() == identity.split(":", 1)[1]
                ),
                None,
            )
        else:
            return await _company_page_posting(client, url, identity, now)
        if posting is None:
            raise ImportFetchError(
                "ATS did not return the exact posting. It may be closed or incomplete."
            )
        if identity != external_identity(posting.url):
            raise ImportFetchError(
                "ATS response does not match the requested posting ID"
            )
        return posting


async def _company_page_posting(
    client: ImportHTTPClient, url: str, identity: str | None, now: datetime
) -> JobPosting:
    greenhouse = identity if identity and identity.startswith("greenhouse:") else None
    tried: tuple[str, ...] = ()
    if greenhouse:
        tried = _greenhouse_url_tokens(url)
        posting = await _greenhouse_posting(client, tried, greenhouse, now)
        if posting is not None:
            return posting
    response = await client.get(url)
    if greenhouse:
        page_tokens = [
            token
            for token in _greenhouse_page_tokens(response.text)
            if token not in tried
        ][: GREENHOUSE_BOARD_ATTEMPTS - len(tried)]
        posting = await _greenhouse_posting(client, page_tokens, greenhouse, now)
        if posting is not None:
            return posting
    if response.status_code in {404, 410}:
        raise ATSFetchError(
            "Posting is closed",
            slug="",
            vendor="web",
            status_code=response.status_code,
        )
    response.raise_for_status()
    if identity and identity != external_identity(str(response.url)):
        raise ImportFetchError("Redirect changed the requested posting identity")
    return _structured_posting(response.text, str(response.url), now)


def _greenhouse_url_tokens(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    labels = (parsed.hostname or "").split(".")[:-1]
    candidates = [
        re.sub(r"^(?:careers|jobs)|(?:careers|jobs)$", "", label)
        for label in labels
        if label not in _GENERIC_HOST_LABELS
    ]
    candidates.extend(
        segment.lower().replace("-", "")
        for segment in parsed.path.split("/")
        if not segment.isdecimal() and segment.lower() not in _GENERIC_PATH_SEGMENTS
    )
    return _board_tokens(candidates)[:GREENHOUSE_BOARD_ATTEMPTS]


def _greenhouse_page_tokens(html: str) -> tuple[str, ...]:
    return _board_tokens(
        match[1].lower() for match in _GREENHOUSE_PAGE_TOKEN.finditer(html)
    )


def _board_tokens(candidates: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token for token in candidates if re.fullmatch(r"[a-z0-9]{2,64}", token)
        )
    )


async def _greenhouse_posting(
    client: ImportHTTPClient,
    tokens: Iterable[str],
    identity: str,
    now: datetime,
) -> JobPosting | None:
    job_id = identity.split(":", 1)[1]
    for token in tokens:
        try:
            posting = await _ats_greenhouse.fetch_job(
                client, token, job_id, discovered_at=now, timeout=8
            )
        except ATSFetchError:
            continue
        # A custom absolute_url carries no identity; a different one is a mismatch.
        if (
            posting is not None
            and posting.canonical_id == job_id
            and external_identity(posting.url) in {identity, None}
        ):
            return posting
    return None


def _structured_records(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        records.extend(_job_records(payload))
    return records


def _job_records(payload: object) -> list[dict[str, Any]]:
    records = []
    pending = payload if isinstance(payload, list) else [payload]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            records.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            pending.extend(graph)
    return records


def _structured_posting(html: str, url: str, now: datetime) -> JobPosting:
    records = _structured_records(html)
    if len(records) != 1:
        raise ImportFetchError(
            "Page must contain exactly one structured JobPosting. "
            "Search, login, and unsupported pages cannot be imported."
        )
    record = records[0]
    if record.get("url"):
        stated_url = record["url"]
        if not isinstance(stated_url, str) or posting_url(
            urljoin(url, stated_url)
        ) != posting_url(url):
            raise ImportFetchError(
                "Structured posting URL does not match the fetched page"
            )
    title = _required_text(record.get("title"), "title")
    description = _required_text(record.get("description"), "description")
    organization = record.get("hiringOrganization")
    company = _required_text(
        organization.get("name") if isinstance(organization, dict) else None,
        "hiring organization",
    )
    expired = None
    if record.get("validThrough"):
        try:
            expiry = datetime.fromisoformat(
                str(record["validThrough"]).replace("Z", "+00:00")
            )
            if expiry.replace(tzinfo=expiry.tzinfo or UTC) < now:
                expired = now
        except ValueError:
            raise ImportFetchError("Posting expiration date is invalid") from None
    jd_text = html_to_text(description)
    normalized = posting_url(url)
    return JobPosting(
        platform="url_import",
        canonical_id=normalized,
        url=normalized,
        title=title.strip(),
        company=company.strip(),
        location="",
        discovered_at=now,
        jd_text=jd_text,
        jd_quality=assess_quality(jd_text),
        enriched_at=now,
        enrich_source="json-ld",
        closed_at=expired,
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImportFetchError(f"Structured posting is missing {label}")
    return value.strip()


def _complete_source(row: sqlite3.Row) -> bool:
    return all(
        row[field] and row[field].strip()
        for field in ("title", "company", "jd_text", "url")
    )
