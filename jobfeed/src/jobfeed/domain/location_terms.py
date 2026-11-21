"""Allow-list locations in the forms job sites write them."""

from __future__ import annotations

import re

_LOCATION_NOTE = re.compile(
    r"\s*\([^)]*\b(?:prefer\w*|ideal\w*|primary|if possible|optional)\b[^)]*\)\s*$",
    re.IGNORECASE,
)
_LOCATION_PAREN = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def location_terms(values: list[str]) -> list[str]:
    """Return allow-list locations in the forms job sites write them.

    Profiles say "Remote (US)" or "Seattle, WA (preferred)"; sites say
    "Remote, US" and "Seattle, WA". A note in parentheses is dropped, and a
    place in parentheses also matches after a comma.

    Args:
        values: Allow-list entries as saved.

    Returns:
        The entries and their site forms, without blanks or repeats.
    """
    terms: list[str] = []
    for value in values:
        entry = _LOCATION_NOTE.sub("", value.strip())
        terms.append(entry)
        if match := _LOCATION_PAREN.match(entry):
            terms.append(f"{match[1]}, {match[2]}")
    return [term for term in dict.fromkeys(terms) if term]


__all__ = ["location_terms"]
