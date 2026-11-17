"""Companies the user does not apply to, whatever a posting's fit."""

from __future__ import annotations

# The user does not apply to these brands for now (2026-10-16).
EXCLUDED_BRANDS = ("tiktok", "bytedance", "capcut", "lemon8")


def excluded_brand(company: str) -> bool:
    """Tell whether a company name carries an excluded brand.

    Args:
        company: Company name from a posting.

    Returns:
        True when the name contains an excluded brand.
    """
    return any(brand in company.casefold() for brand in EXCLUDED_BRANDS)
