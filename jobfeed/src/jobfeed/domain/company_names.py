"""One company's identity across the different ways postings write its name."""

from __future__ import annotations

import re

_COMPANY_SUFFIXES = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "llc",
        "ltd",
        "limited",
        "technologies",
    }
)


def normalize_company_name(value: str) -> str:
    """Normalize punctuation and legal suffixes for exact name matching.

    Args:
        value: Raw employer name.

    Returns:
        Lowercase whitespace-separated identity key.
    """
    words = re.findall(r"[a-z0-9]+", value.casefold())
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)
