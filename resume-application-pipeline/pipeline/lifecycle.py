"""Application lifecycle states and allowed transitions."""

from __future__ import annotations

from enum import StrEnum


class ApplicationState(StrEnum):
    QUEUED = "Queued"
    DISPATCHING = "Dispatching"
    TAILORING = "Tailoring"
    TAILORING_PAUSED = "Tailoring Paused"
    RESUME_READY = "Resume Ready"
    DEFERRED = "Deferred"
    APPLYING = "Applying"
    READY_FOR_REVIEW = "Ready for Review"
    REVIEWING = "Reviewing"
    AWAITING_APPROVAL = "Awaiting Approval"
    SUBMIT_AUTHORIZED = "Submit Authorized"
    SUBMITTING = "Submitting"
    NEEDS_INPUT = "Needs Input"
    NEEDS_VERIFICATION = "Needs Verification"
    RETRYING = "Retrying"
    RETRY_LATER = "Retry Later"
    PLATFORM_WAIT = "Platform Wait"
    SUBMISSION_UNKNOWN = "Submission Unknown"
    SUBMITTED = "Submitted"
    SKIPPED = "Skipped"
    CANCELLED = "Cancelled"
    UNAVAILABLE = "Unavailable"
    ALREADY_APPLIED = "Already Applied"


# Waiting on the user or a verification step: the owner and pages stay, but an
# idle wait does not hold a dispatch slot (2026-10-15 user decision).
WAITING_ON_USER = frozenset({ApplicationState.NEEDS_INPUT, ApplicationState.NEEDS_VERIFICATION})

TERMINAL_STATES = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.SKIPPED,
        ApplicationState.CANCELLED,
        ApplicationState.UNAVAILABLE,
        ApplicationState.ALREADY_APPLIED,
    }
)

ALLOWED_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.QUEUED: frozenset(
        {
            ApplicationState.DISPATCHING,
            ApplicationState.RESUME_READY,
            ApplicationState.DEFERRED,
            ApplicationState.SKIPPED,
            ApplicationState.CANCELLED,
            ApplicationState.UNAVAILABLE,
            ApplicationState.ALREADY_APPLIED,
        }
    ),
    ApplicationState.DISPATCHING: frozenset(
        {
            ApplicationState.TAILORING_PAUSED,
            ApplicationState.SKIPPED,
            ApplicationState.QUEUED,
            ApplicationState.TAILORING,
            ApplicationState.CANCELLED,
            ApplicationState.UNAVAILABLE,
        }
    ),
    ApplicationState.TAILORING: frozenset(
        {
            ApplicationState.TAILORING_PAUSED,
            ApplicationState.SKIPPED,
            ApplicationState.QUEUED,
            ApplicationState.RESUME_READY,
            ApplicationState.CANCELLED,
            ApplicationState.UNAVAILABLE,
        }
    ),
    # Human reconciliation only: never automatically retry an uncertain dispatch.
    ApplicationState.TAILORING_PAUSED: frozenset(
        {ApplicationState.TAILORING, ApplicationState.SKIPPED, ApplicationState.CANCELLED}
    ),
    ApplicationState.RESUME_READY: frozenset(
        {
            ApplicationState.APPLYING,
            ApplicationState.DEFERRED,
            ApplicationState.SKIPPED,
            ApplicationState.CANCELLED,
            ApplicationState.UNAVAILABLE,
        }
    ),
    ApplicationState.DEFERRED: frozenset(
        {ApplicationState.RESUME_READY, ApplicationState.APPLYING, ApplicationState.CANCELLED}
    ),
    ApplicationState.APPLYING: frozenset(
        {
            ApplicationState.READY_FOR_REVIEW,
            ApplicationState.NEEDS_INPUT,
            ApplicationState.CANCELLED,
            ApplicationState.UNAVAILABLE,
            ApplicationState.ALREADY_APPLIED,
        }
    ),
    ApplicationState.NEEDS_INPUT: frozenset(
        {ApplicationState.APPLYING, ApplicationState.CANCELLED}
    ),
    ApplicationState.READY_FOR_REVIEW: frozenset({ApplicationState.REVIEWING}),
    ApplicationState.REVIEWING: frozenset(
        {
            ApplicationState.APPLYING,
            ApplicationState.NEEDS_INPUT,
            ApplicationState.AWAITING_APPROVAL,
            ApplicationState.CANCELLED,
        }
    ),
    ApplicationState.AWAITING_APPROVAL: frozenset(
        {
            ApplicationState.APPLYING,
            ApplicationState.SUBMIT_AUTHORIZED,
            ApplicationState.CANCELLED,
        }
    ),
    ApplicationState.SUBMIT_AUTHORIZED: frozenset({ApplicationState.SUBMITTING}),
    ApplicationState.SUBMITTING: frozenset(
        {
            ApplicationState.SUBMITTED,
            ApplicationState.UNAVAILABLE,
            ApplicationState.ALREADY_APPLIED,
            ApplicationState.NEEDS_VERIFICATION,
            ApplicationState.RETRYING,
            ApplicationState.SUBMISSION_UNKNOWN,
        }
    ),
    ApplicationState.NEEDS_VERIFICATION: frozenset(
        {ApplicationState.SUBMITTING, ApplicationState.CANCELLED}
    ),
    ApplicationState.RETRYING: frozenset(
        {
            ApplicationState.SUBMITTING,
            ApplicationState.RETRY_LATER,
            ApplicationState.SUBMISSION_UNKNOWN,
        }
    ),
    ApplicationState.PLATFORM_WAIT: frozenset(
        {ApplicationState.APPLYING, ApplicationState.CANCELLED, ApplicationState.NEEDS_INPUT}
    ),
    ApplicationState.RETRY_LATER: frozenset(
        {ApplicationState.SUBMITTING, ApplicationState.CANCELLED}
    ),
    ApplicationState.SUBMISSION_UNKNOWN: frozenset(
        {
            ApplicationState.SUBMITTING,
            ApplicationState.SUBMITTED,
            ApplicationState.ALREADY_APPLIED,
        }
    ),
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: ApplicationState, target: ApplicationState) -> None:
    if target == ApplicationState.PLATFORM_WAIT and current in {ApplicationState.APPLYING, ApplicationState.REVIEWING, ApplicationState.READY_FOR_REVIEW, ApplicationState.AWAITING_APPROVAL, ApplicationState.SUBMIT_AUTHORIZED, ApplicationState.SUBMITTING, ApplicationState.SUBMISSION_UNKNOWN}:
        return
    if target in {ApplicationState.ALREADY_APPLIED, ApplicationState.UNAVAILABLE, ApplicationState.SUBMISSION_UNKNOWN} and current not in TERMINAL_STATES:
        return
    if target == ApplicationState.NEEDS_INPUT and current in {ApplicationState.NEEDS_VERIFICATION, ApplicationState.SUBMITTING, ApplicationState.SUBMISSION_UNKNOWN}:
        return
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"cannot transition application from {current} to {target}")
