"""Discovery origins: Getro portfolio boards, the a16z portfolio, and HN hiring threads.

Each origin pairs pure parsers for its public pages with one ``list_work``
call. Jobs never come from these pages; they only name employer ATS boards.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from functools import partial
from urllib.parse import urlsplit

from jobfeed.adapters.sources._ats_discovery import (
    Board,
    Lookup,
    Origin,
    OriginWork,
    PoliteHttp,
)
from jobfeed.adapters.sources._bootstrap_aggregators import (
    count_ats_slugs,
    extract_ats_slugs,
)
from jobfeed.config import SourcesATSDiscoveryConfig

GETRO_API = "https://api.getro.com/api/v2/collections/{network_id}/search/{kind}"
A16Z_SITEMAP_URL = "https://jobs.a16z.com/sitemap.xml"
HN_SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&hitsPerPage=20"
)
HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{item_id}"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_A16Z_PAGE_RE = re.compile(r"^https://jobs\.a16z\.com/(company|jobs)/([^/?#]+)/?$")
_HN_HIRING_TITLE_PREFIX = "Ask HN: Who is hiring?"
# The server caps a page below this; asking for more costs nothing.
_GETRO_PAGE_SIZE = 20
# A guard against an API that ignores the page number and repeats itself.
_GETRO_MAX_PAGES = 500


@dataclass(frozen=True)
class GetroCompany:
    """A portfolio company as Getro's company search lists it."""

    id: int
    name: str
    active_jobs: int


def getro_network_id(page: str) -> int:
    """Read the Getro network id from a board's server-rendered ``/jobs`` page.

    Args:
        page: HTML of ``<board>/jobs``.

    Returns:
        ``props.pageProps.network.id`` from the ``__NEXT_DATA__`` script.

    Raises:
        ValueError: When the page carries no network id.
    """
    match = _NEXT_DATA_RE.search(page)
    if match is None:
        raise ValueError("board page has no __NEXT_DATA__ script")
    network: object = json.loads(match.group(1))
    for key in ("props", "pageProps", "network", "id"):
        network = network.get(key) if isinstance(network, dict) else None
    # Boards serve the id as a JSON string ("120"); the API path takes digits.
    if isinstance(network, str) and network.isdigit():
        return int(network)
    if not isinstance(network, int) or isinstance(network, bool):
        raise ValueError("board page names no Getro network id")
    return network


def getro_companies(payload: object) -> tuple[list[GetroCompany], int]:
    """Parse one page of Getro's company search.

    Args:
        payload: Decoded ``search/companies`` JSON.

    Returns:
        The page's companies and the total company count.

    Raises:
        ValueError: When the payload is not a company search result.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    rows = results.get("companies") if isinstance(results, dict) else None
    if not isinstance(results, dict) or not isinstance(rows, list):
        raise ValueError("Getro company search returned no results.companies")
    companies = [
        GetroCompany(
            id=row["id"],
            name=str(row.get("name") or row["id"]),
            active_jobs=int(row.get("active_jobs_count") or 0),
        )
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    ]
    count = results.get("count")
    return companies, count if isinstance(count, int) else 0


def getro_job_urls(payload: object) -> list[str]:
    """List the apply URLs in one page of Getro's job search, in order.

    Args:
        payload: Decoded ``search/jobs`` JSON.

    Returns:
        Each job's ``url``; empty when the payload has none.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    jobs = results.get("jobs") if isinstance(results, dict) else None
    if not isinstance(jobs, list):
        return []
    return [
        job["url"]
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("url"), str)
    ]


def first_board(urls: list[str]) -> Board | None:
    """Return the board of the first URL that points at a supported ATS.

    Args:
        urls: Apply URLs in listing order.

    Returns:
        That board, or None when no URL is on a supported ATS.
    """
    for url in urls:
        if pairs := extract_ats_slugs(url):
            return Board(*min(pairs))
    return None


def a16z_company_pages(sitemap: str) -> dict[str, str]:
    """Map each a16z portfolio company to one page that links its board.

    The sitemap lists ``/company/<x>`` and ``/jobs/<x>`` for many companies;
    both embed the same board, and ``/jobs/<x>`` links it more often.

    Args:
        sitemap: ``jobs.a16z.com/sitemap.xml`` text.

    Returns:
        Company path segment to page URL; index pages are left out.
    """
    pages: dict[str, str] = {}
    for loc in _LOC_RE.findall(sitemap):
        match = _A16Z_PAGE_RE.match(loc)
        if match is None:
            continue
        kind, company = match.groups()
        if kind == "jobs" or company not in pages:
            pages[company] = loc
    return pages


def most_linked_board(page: str) -> Board | None:
    """Return the board a page links most often (ties: alphabetical).

    Args:
        page: Page HTML.

    Returns:
        The most-linked board, or None when the page links none.
    """
    counts = count_ats_slugs(page)
    if not counts:
        return None
    pair = min(counts, key=lambda item: (-counts[item], item))
    return Board(*pair)


def hiring_thread_ids(search: object, limit: int) -> list[str]:
    """Return the newest "Who is hiring?" thread ids from an HN search.

    The whoishiring account also posts "Who wants to be hired?" and
    "Freelancer?" threads; those are skipped. Hits arrive newest first.

    Args:
        search: Decoded Algolia ``search_by_date`` JSON.
        limit: How many threads to keep.

    Returns:
        Up to ``limit`` thread ids, newest first.
    """
    hits = search.get("hits") if isinstance(search, dict) else None
    if not isinstance(hits, list):
        return []
    ids = [
        hit["objectID"]
        for hit in hits
        if isinstance(hit, dict)
        and isinstance(hit.get("title"), str)
        and hit["title"].startswith(_HN_HIRING_TITLE_PREFIX)
        and isinstance(hit.get("objectID"), str)
        and hit["objectID"]
    ]
    return ids[:limit]


def hn_thread_text(item: object) -> str:
    """Flatten an HN Algolia item tree into unescaped comment text.

    Comment HTML arrives entity-escaped (``https:&#x2F;&#x2F;...``), which
    hides ATS URLs from the slug regexes until unescaped.

    Args:
        item: Decoded Algolia ``items/{id}`` JSON (nested ``children``).

    Returns:
        Every node's unescaped ``text`` joined with newlines, depth first;
        "" when the payload is not the expected shape.
    """
    texts: list[str] = []
    stack = [item]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        text = node.get("text")
        if isinstance(text, str):
            texts.append(html.unescape(text))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return "\n".join(texts)


class GetroOrigin:
    """A Getro-hosted VC portfolio board; one lookup per hiring company."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self.name = f"getro:{urlsplit(base_url).hostname}"

    async def list_work(self, http: PoliteHttp) -> OriginWork:
        """List the board's companies; each one with open jobs is a lookup.

        Args:
            http: Paced client.

        Returns:
            Every listed company, and a lookup per company with active jobs.
        """
        network_id = getro_network_id(await http.text(f"{self._base_url}/jobs"))
        url = GETRO_API.format(network_id=network_id, kind="companies")
        seen: dict[int, GetroCompany] = {}
        for page in range(_GETRO_MAX_PAGES):
            body = {
                "hitsPerPage": _GETRO_PAGE_SIZE,
                "page": page,
                "filters": "",
                "query": "",
            }
            companies, count = getro_companies(await http.json(url, body))
            fresh = [c for c in companies if c.id not in seen]
            seen |= {c.id: c for c in fresh}
            if not fresh or len(seen) >= count:
                break
        lookups = tuple(
            Lookup(
                key=f"getro:{network_id}:{company.id}",
                resolve=partial(self._resolve, http, network_id, company.id),
            )
            for company in seen.values()
            if company.active_jobs > 0
        )
        return OriginWork(listed=len(seen), lookups=lookups)

    @staticmethod
    async def _resolve(
        http: PoliteHttp, network_id: int, company_id: int
    ) -> Board | None:
        body = {
            "hitsPerPage": 20,
            "page": 0,
            "filters": {"organization.id": [company_id]},
            "query": "",
        }
        url = GETRO_API.format(network_id=network_id, kind="jobs")
        return first_board(getro_job_urls(await http.json(url, body)))


class A16zOrigin:
    """The a16z portfolio board; one lookup per company page in its sitemap."""

    name = "a16z"

    async def list_work(self, http: PoliteHttp) -> OriginWork:
        """List portfolio companies from the sitemap; each one is a lookup.

        Args:
            http: Paced client.

        Returns:
            A lookup per portfolio company.
        """
        pages = a16z_company_pages(await http.text(A16Z_SITEMAP_URL))
        lookups = tuple(
            Lookup(key=f"a16z:{company}", resolve=partial(self._resolve, http, url))
            for company, url in sorted(pages.items())
        )
        return OriginWork(listed=len(pages), lookups=lookups)

    @staticmethod
    async def _resolve(http: PoliteHttp, url: str) -> Board | None:
        return most_linked_board(await http.text(url))


class HNOrigin:
    """Recent Hacker News "Who is hiring?" threads; boards come straight off them."""

    name = "hn"

    def __init__(self, threads: int) -> None:
        self._threads = threads

    async def list_work(self, http: PoliteHttp) -> OriginWork:
        """Read the newest hiring threads and take every board they link.

        Args:
            http: Paced client.

        Returns:
            The boards linked from the threads; no lookups.

        Raises:
            ValueError: When the search names no hiring thread.
        """
        thread_ids = hiring_thread_ids(await http.json(HN_SEARCH_URL), self._threads)
        if not thread_ids:
            raise ValueError("no 'Who is hiring?' thread in HN search results")
        boards: set[Board] = set()
        for thread_id in thread_ids:
            item = await http.json(HN_ITEM_URL.format(item_id=thread_id))
            boards |= {Board(*pair) for pair in extract_ats_slugs(hn_thread_text(item))}
        return OriginWork(listed=len(boards), boards=frozenset(boards))


def build_origins(config: SourcesATSDiscoveryConfig) -> list[Origin]:
    """Return the origins the configuration turns on.

    Args:
        config: Discovery configuration.

    Returns:
        Getro boards in configured order, then a16z, then Hacker News.
    """
    origins: list[Origin] = [GetroOrigin(url) for url in config.getro_boards]
    if config.a16z:
        origins.append(A16zOrigin())
    if config.hn_threads:
        origins.append(HNOrigin(config.hn_threads))
    return origins


__all__ = [
    "A16Z_SITEMAP_URL",
    "GETRO_API",
    "HN_ITEM_URL",
    "HN_SEARCH_URL",
    "A16zOrigin",
    "GetroCompany",
    "GetroOrigin",
    "HNOrigin",
    "a16z_company_pages",
    "build_origins",
    "first_board",
    "getro_companies",
    "getro_job_urls",
    "getro_network_id",
    "hiring_thread_ids",
    "hn_thread_text",
    "most_linked_board",
]
