"""Which evaluated jobs go on the recruiter sheet, and what the user may do with a row.

The sheet is derived from existing data on every read, so it needs no job or timer
of its own. The autopilot hold reads the same rows, so the sheet and the hold cannot
disagree.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from jobfeed.domain.company_names import normalize_company_name
from jobfeed.domain.company_policy import excluded_brand

# Regular expressions matched case-insensitively. New York State is not New York City:
# Buffalo, Monroe, Great Neck and "Albany, New York" must not match, so a bare
# "New York" counts only where it starts a location, never as the state after a comma.
DEFAULT_LOCATIONS: tuple[str, ...] = (
    r"\bnew york city\b",
    r"\bnyc\b",
    r"\bnew york,\s*(?:ny|new york)\b",
    r"(?:^|[;|•/(-]\s*|\bor\s+)new york\b(?!\s+(?:mills|state))",
    r"\b(?:manhattan|brooklyn|queens|bronx|staten island|tribeca),?\s+ny\b",
    r"^manhattan$",
    r"\blong island city\b",
)
MAX_RECRUITER_LENGTH = 200
MAX_NOTE_LENGTH = 4000
_INTERNSHIP = re.compile(r"\b(?:interns?|internships?|co-?ops?)\b", re.IGNORECASE)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SHORT_TEXT = 140
_SHORT_ITEMS = 3
_MONTHS_IN_YEAR = 12
_ANNOUNCED_ON = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
_YEAR_MONTH_LENGTH = 7
_FUNDING_RECHECK_DAYS = 30
_FUNDING_NOT_FOUND = "None found"
_AMOUNT_UNITS = ((1e9, "B"), (1e6, "M"), (1e3, "K"))


class SheetStatus(StrEnum):
    """What the user did with a row. No stored row means ``TO_ASK``."""

    TO_ASK = "to_ask"
    ASKED = "asked"
    WITH_RECRUITER = "with_recruiter"
    PASSED = "passed"
    APPLY_MYSELF = "apply_myself"


# A row a recruiter is handling never silently returns to "to ask", and a job the user
# may have applied to directly never moves straight to a recruiter.
TRANSITIONS: Mapping[SheetStatus, frozenset[SheetStatus]] = {
    SheetStatus.TO_ASK: frozenset(
        {
            SheetStatus.ASKED,
            SheetStatus.WITH_RECRUITER,
            SheetStatus.PASSED,
            SheetStatus.APPLY_MYSELF,
        }
    ),
    SheetStatus.ASKED: frozenset(
        {
            SheetStatus.TO_ASK,
            SheetStatus.WITH_RECRUITER,
            SheetStatus.PASSED,
            SheetStatus.APPLY_MYSELF,
        }
    ),
    SheetStatus.WITH_RECRUITER: frozenset(
        {SheetStatus.ASKED, SheetStatus.PASSED, SheetStatus.APPLY_MYSELF}
    ),
    SheetStatus.PASSED: frozenset(
        {SheetStatus.TO_ASK, SheetStatus.ASKED, SheetStatus.APPLY_MYSELF}
    ),
    SheetStatus.APPLY_MYSELF: frozenset(
        {SheetStatus.TO_ASK, SheetStatus.ASKED, SheetStatus.PASSED}
    ),
}


def default_location_patterns() -> tuple[re.Pattern[str], ...]:
    """Compile the default New York City location patterns.

    Returns:
        The compiled patterns.
    """
    return tuple(re.compile(text, re.IGNORECASE) for text in DEFAULT_LOCATIONS)


@dataclass(frozen=True)
class SheetPolicy:
    """The configured rules for what qualifies."""

    locations: tuple[re.Pattern[str], ...] = field(
        default_factory=default_location_patterns
    )
    min_fit: int = 78
    max_major_gaps: int = 2
    excluded_companies: tuple[str, ...] = ()
    min_pay: int = 130000
    funding_within_months: int = 12


@dataclass(frozen=True)
class QueueEntry:
    """One source posting's row in the application queue."""

    state: str
    owner: str | None


@dataclass(frozen=True)
class FundingRound:
    """A funding round a lookup verified against its search results."""

    name: str
    amount_usd: float | None
    announced_on: date
    source_url: str


@dataclass(frozen=True)
class FundingRecord:
    """A company's last funding check. No round means the check found none."""

    round: FundingRound | None
    checked_on: date


@dataclass(frozen=True)
class SheetJob:
    """The facts about one real job that selection and display need."""

    real_job_id: int
    source_job_ids: tuple[int, ...]
    company: str
    title: str
    location: str
    url: str
    verdict: str | None = None
    fit: int | None = None
    critical_gaps: int = 0
    major_gaps: tuple[str, ...] = ()
    strengths: tuple[str, ...] = ()
    closed: bool = False
    ignored: bool = False
    applied: bool = False
    queue: tuple[QueueEntry, ...] = ()
    evaluated_at: str | None = None
    pay: int | None = None
    funding: FundingRecord | None = None

    @property
    def company_key(self) -> str:
        """Name the company for its funding record.

        Returns:
            The normalized company name, blank when the name has no letters or digits.
        """
        return normalize_company_name(self.company)


@dataclass(frozen=True)
class SheetDecision:
    """What the user stored for one real job."""

    status: SheetStatus = SheetStatus.TO_ASK
    recruiter: str = ""
    note: str = ""
    updated_at: str | None = None


@dataclass(frozen=True)
class SheetRow:
    """One row of the sheet. ``real_job_id`` is its stable key."""

    real_job_id: int
    source_job_ids: tuple[int, ...]
    company: str
    title: str
    location: str
    fit: int | None
    verdict: str | None
    why: str
    watch_out: str
    url: str | None
    status: SheetStatus
    recruiter: str
    note: str
    updated_at: str | None
    status_updated_at: str | None
    closed: bool
    applied: bool
    queue_state: str | None
    qualifies: bool
    pay: int | None
    funding: str
    funding_source: str | None
    funding_pending: bool


@dataclass(frozen=True)
class SheetEdit:
    """A requested change to one row. ``status`` is raw text until checked.

    A recruiter or note of None keeps the row's current value.
    """

    real_job_id: int
    status: str
    recruiter: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class EditProblem:
    """Why one edit was refused."""

    code: str
    message: str


def _months_before(day: date, months: int) -> date:
    """Step back whole calendar months, clamping to the last day of a short month."""
    year, month_index = divmod(day.year * _MONTHS_IN_YEAR + day.month - 1 - months, 12)
    month = month_index + 1
    next_month = date(year + month // _MONTHS_IN_YEAR, month % _MONTHS_IN_YEAR + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _funding_passes(job: SheetJob, policy: SheetPolicy, today: date) -> bool:
    """A job with no record passes, so it stays held until its check has run."""
    if job.funding is None:
        return True
    funding = job.funding.round
    if funding is None:
        return False
    earliest = _months_before(today, policy.funding_within_months)
    return earliest <= funding.announced_on <= today


_Rule = tuple[str, Callable[[SheetJob, SheetPolicy, date], bool]]
_RULES_BEFORE_PAY: tuple[_Rule, ...] = (
    ("verdict_not_apply", lambda job, _, __: job.verdict == "apply"),
    (
        "outside_locations",
        lambda job, policy, _: any(p.search(job.location) for p in policy.locations),
    ),
    (
        "fit_below_minimum",
        lambda job, policy, _: job.fit is not None and job.fit >= policy.min_fit,
    ),
    ("critical_gap", lambda job, _, __: job.critical_gaps == 0),
    (
        "too_many_major_gaps",
        lambda job, policy, _: len(job.major_gaps) <= policy.max_major_gaps,
    ),
    ("internship", lambda job, _, __: not _INTERNSHIP.search(job.title)),
    ("excluded_company", lambda job, policy, _: not _company_excluded(job, policy)),
    ("closed", lambda job, _, __: not job.closed),
    ("ignored", lambda job, _, __: not job.ignored),
    ("applied", lambda job, _, __: not job.applied),
    (
        "queue_state",
        lambda job, _, __: all(
            entry.state == "Queued" and not entry.owner for entry in job.queue
        ),
    ),
)
# New York law requires a stated range, so unstated pay does not pass.
_PAY_RULE: _Rule = (
    "pay_too_low",
    lambda job, policy, _: job.pay is not None and job.pay >= policy.min_pay,
)
_RULES: tuple[_Rule, ...] = (
    *_RULES_BEFORE_PAY,
    _PAY_RULE,
    ("no_recent_funding", _funding_passes),
)


def _company_excluded(job: SheetJob, policy: SheetPolicy) -> bool:
    company = job.company.casefold()
    return excluded_brand(job.company) or any(
        name.casefold() in company for name in policy.excluded_companies
    )


def disqualification(job: SheetJob, policy: SheetPolicy, today: date) -> str | None:
    """Name the first rule a job fails.

    Args:
        job: The job's facts.
        policy: The configured rules.
        today: The date the funding window ends on.

    Returns:
        The rule's name, or None when the job qualifies.
    """
    return next(
        (name for name, passes in _RULES if not passes(job, policy, today)), None
    )


def needs_pay(job: SheetJob, policy: SheetPolicy, today: date) -> bool:
    """Tell whether pay could still decide a job, which is worth reading its posting.

    Args:
        job: The job's facts.
        policy: The configured rules.
        today: The date the funding window ends on.

    Returns:
        Whether the job passes every rule before the pay rule.
    """
    return all(passes(job, policy, today) for _, passes in _RULES_BEFORE_PAY)


def funding_lookups_needed(
    jobs: Iterable[SheetJob],
    records: Mapping[str, FundingRecord],
    policy: SheetPolicy,
    today: date,
) -> list[str]:
    """Name the companies whose funding check should run next.

    Only funding can still decide such a job. A stale record stays in force until a
    new check replaces it.

    Args:
        jobs: Candidate jobs.
        records: Stored funding checks by company key.
        policy: The configured rules.
        today: The date staleness counts from.

    Returns:
        Distinct company keys with no record or a record over 30 days old, best fit
        first. A company name that normalizes to nothing has no key, so it is left out.
    """
    candidates = [
        job
        for job in jobs
        if job.company_key
        and needs_pay(job, policy, today)
        and _PAY_RULE[1](job, policy, today)
        and _check_is_due(records.get(job.company_key), today)
    ]
    candidates.sort(
        key=lambda job: (-(job.fit or 0), job.company.casefold(), job.real_job_id)
    )
    return list(dict.fromkeys(job.company_key for job in candidates))


def _check_is_due(record: FundingRecord | None, today: date) -> bool:
    return record is None or (today - record.checked_on).days > _FUNDING_RECHECK_DAYS


def parse_announced_on(text: str) -> date:
    """Read a funding date. YYYY-MM means the first day of that month.

    Args:
        text: The date text.

    Returns:
        The date.

    Raises:
        ValueError: If the text is neither form or names no real date.
    """
    if not _ANNOUNCED_ON.fullmatch(text):
        raise ValueError(f"not a date: {text!r}")
    return date.fromisoformat(text if len(text) > _YEAR_MONTH_LENGTH else f"{text}-01")


def _amount_text(amount: float) -> str:
    for size, unit in _AMOUNT_UNITS:
        if amount >= size:
            return f"${amount / size:.4g}{unit}"
    return f"${amount:.4g}"


def _funding_text(record: FundingRecord | None) -> str:
    if record is None:
        return ""
    if record.round is None:
        return _FUNDING_NOT_FOUND
    funding = record.round
    amount = [] if funding.amount_usd is None else [_amount_text(funding.amount_usd)]
    return ", ".join([funding.name, *amount, funding.announced_on.isoformat()])


def _short(items: Iterable[str]) -> str:
    texts = [" ".join(item.split()) for item in items]
    return "; ".join(
        text if len(text) <= _SHORT_TEXT else text[: _SHORT_TEXT - 1].rstrip() + "…"
        for text in texts[:_SHORT_ITEMS]
    )


def _safe_url(url: str) -> str | None:
    return url if url.lower().startswith(("https://", "http://")) else None


def _row(job: SheetJob, decision: SheetDecision, qualifies: bool) -> SheetRow:
    times = [time for time in (decision.updated_at, job.evaluated_at) if time]
    states = [entry.state for entry in job.queue]
    funding = job.funding.round if job.funding else None
    return SheetRow(
        real_job_id=job.real_job_id,
        source_job_ids=job.source_job_ids,
        company=job.company,
        title=job.title,
        location=job.location,
        fit=job.fit,
        verdict=job.verdict,
        why=_short(job.strengths),
        watch_out=_short(job.major_gaps),
        url=_safe_url(job.url),
        status=decision.status,
        recruiter=decision.recruiter,
        note=decision.note,
        updated_at=max(times) if times else None,
        status_updated_at=decision.updated_at,
        closed=job.closed,
        applied=job.applied,
        queue_state=states[0] if len(set(states)) == 1 else None,
        qualifies=qualifies,
        pay=job.pay,
        funding=_funding_text(job.funding),
        funding_source=funding.source_url if funding else None,
        funding_pending=qualifies and job.funding is None,
    )


def build_sheet(
    jobs: Iterable[SheetJob],
    decisions: Mapping[int, SheetDecision],
    policy: SheetPolicy,
    today: date,
) -> list[SheetRow]:
    """Select the sheet: every job that qualifies or has a stored decision.

    A stored decision keeps its job on the sheet after the job stops qualifying, so a
    job a recruiter is handling never silently vanishes.

    Args:
        jobs: Candidate jobs.
        decisions: Stored decisions by real job ID.
        policy: The configured rules.
        today: The date the funding window ends on.

    Returns:
        The rows, best fit first.
    """
    rows = []
    for job in jobs:
        qualifies = disqualification(job, policy, today) is None
        decision = decisions.get(job.real_job_id)
        if qualifies or decision is not None:
            rows.append(_row(job, decision or SheetDecision(), qualifies))
    return sorted(
        rows,
        key=lambda row: (
            -(row.fit if row.fit is not None else -1),
            row.company.casefold(),
            row.real_job_id,
        ),
    )


def held_source_ids(rows: Iterable[SheetRow]) -> frozenset[int]:
    """Source postings autopilot must not apply to: all rows but apply_myself.

    Args:
        rows: The sheet.

    Returns:
        The source job IDs of every held row.
    """
    return frozenset(
        source_id
        for row in rows
        if row.status is not SheetStatus.APPLY_MYSELF
        for source_id in row.source_job_ids
    )


def edit_problem(edit: SheetEdit, current: SheetDecision) -> EditProblem | None:
    """Check one edit against the status table.

    Args:
        edit: The requested change.
        current: The row's decision now.

    Returns:
        Why the edit is refused, or None when it may be saved.
    """
    try:
        status = SheetStatus(edit.status)
    except ValueError:
        allowed = ", ".join(SheetStatus)
        return EditProblem(
            "unknown_status", f"Status must be one of {allowed}, not {edit.status!r}."
        )
    if (
        len(edit.recruiter or "") > MAX_RECRUITER_LENGTH
        or len(edit.note or "") > MAX_NOTE_LENGTH
    ):
        return EditProblem(
            "too_long",
            f"Recruiter is limited to {MAX_RECRUITER_LENGTH} characters "
            f"and note to {MAX_NOTE_LENGTH}.",
        )
    if status is not current.status and status not in TRANSITIONS[current.status]:
        return EditProblem(
            "invalid_transition",
            f"A row cannot go from {current.status} to {status}.",
        )
    return None


def neutralize_cell(value: str) -> str:
    """Stop a spreadsheet from running a cell as a formula.

    Args:
        value: Cell text, which may come from a job posting.

    Returns:
        The text, prefixed with an apostrophe when it could start a formula.
    """
    return f"'{value}" if value.lstrip(" \n").startswith(_FORMULA_PREFIXES) else value
