"""Normalize posting URLs and derive import identity without network access."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobfeed.domain.external_identity import external_identity

MAX_URL_LENGTH = 8192


@dataclass(frozen=True)
class PostingURLKey:
    """A normalized posting URL and its optional native ATS identity."""

    normalized_url: str
    native_identity: str | None

    @property
    def concurrency_key(self) -> str:
        """Return the shared import key for native aliases or normalized URLs.

        Returns:
            The native identity prefixed with native: when known, otherwise the
            normalized URL prefixed with url:.
        """
        return (
            f"native:{self.native_identity}"
            if self.native_identity
            else f"url:{self.normalized_url}"
        )

    @classmethod
    def parse(cls, url: str) -> PostingURLKey:
        """Parse a posting identity and reject unsafe literal destinations.

        Hostnames are parsed without DNS resolution. The HTTP adapter must guard
        resolved destinations separately before each request.

        Args:
            url: One HTTP or HTTPS posting URL, at most 8192 characters.

        Returns:
            A normalized URL and its native identity when recognized.

        Raises:
            ValueError: The URL is malformed or violates posting URL restrictions.
        """
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if (
            len(url) > MAX_URL_LENGTH
            or parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise ValueError("Supply a public HTTP or HTTPS posting URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Supply a public HTTP or HTTPS posting URL")
        identity = external_identity(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and not (
                identity and key.lower() in {"source", "ref", "referrer", "gh_src"}
            )
        ]
        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                urlencode(sorted(query)),
                "",
            )
        )
        return cls(normalized, identity)


def posting_url(url: str) -> str:
    """Normalize a URL with the posting identity rules.

    Args:
        url: Posting URL to parse.

    Returns:
        The URL without fragments or ignored tracking parameters.

    Raises:
        ValueError: The URL is malformed or violates posting URL restrictions.
    """
    return PostingURLKey.parse(url).normalized_url
