"""Public pairing and focus messages for existing application tabs."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
BindingState = Literal["connected", "disconnected", "tab_closed", "page_changed"]
FocusStatus = Literal["focused", "tab_closed", "page_changed", "focus_failed"]


class TabMessage(BaseModel):
    """Strict base model for application tab messages; unknown fields are rejected."""

    model_config = ConfigDict(strict=True, extra="forbid")


class ApplicationTabBinding(TabMessage):
    """A job's tab binding as shown to the dashboard."""

    job_id: int
    owner_thread_id: str
    binding_id: str
    state: BindingState


class ApplicationTabsState(TabMessage):
    """CSRF token, tab bindings and profile probe availability for the dashboard."""

    csrf_token: str
    bindings: list[ApplicationTabBinding]
    profile_probe_available: bool = False
    profile_probe_reason: str | None = None


class ApplicationTabPairingRequest(TabMessage):
    """Request to pair or reopen a job's recorded tab for one owner thread."""

    owner_thread_id: Identifier


class ApplicationTabCandidate(TabMessage):
    """A browser tab, by native ID and URL, that may hold an application."""

    native_tab_id: Annotated[int, Field(gt=0)]
    url: Annotated[str, Field(min_length=1, max_length=4096)]


class ApplicationProfileProbeRequest(TabMessage):
    """Candidate tabs to look for in the extension's Chrome profile."""

    candidates: Annotated[
        list[ApplicationTabCandidate], Field(min_length=1, max_length=16)
    ]


class ApplicationProfileProof(TabMessage):
    """Short-lived proof of which candidate tabs the extension's profile holds."""

    profile_proof: str
    expires_at: str
    matches: list[ApplicationTabCandidate]


class ApplicationTabAutoConnectRequest(ApplicationTabCandidate):
    """Request to bind one proven tab to a job for an owner thread."""

    owner_thread_id: Identifier
    profile_proof: Identifier


class ApplicationProfileValidateRequest(TabMessage):
    """Request to check that a profile proof is still valid."""

    profile_proof: Identifier


class ApplicationProfileValidation(TabMessage):
    """Confirmation that a profile proof is valid, with its expiry."""

    valid: Literal[True] = True
    expires_at: str


class ApplicationTabPairing(TabMessage):
    """A pairing code the extension redeems to bind a tab, with its expiry."""

    code: str
    expires_at: str


class ApplicationTabConnectRequest(TabMessage):
    """The extension's request to redeem a pairing code for a selected tab."""

    code: Identifier
    browser_session_id: Identifier
    native_tab_id: Annotated[int, Field(gt=0)]
    url: Annotated[str, Field(min_length=1, max_length=4096)]


class ApplicationTabCredentials(TabMessage):
    """Binding ID and session token issued to the extension for one tab."""

    binding_id: str
    session_token: str


class ApplicationTabHello(TabMessage):
    """First socket message proving the extension holds a binding's credentials."""

    type: Literal["hello"]
    binding_id: Identifier
    session_token: Identifier
    browser_session_id: Identifier


class ApplicationTabFocusRequest(TabMessage):
    """Request to focus a bound tab for an owner thread."""

    owner_thread_id: Identifier
    binding_id: Identifier


class ApplicationTabFocusResult(TabMessage):
    """Outcome of a focus request as reported by the extension."""

    status: FocusStatus


class ApplicationTabReply(TabMessage):
    """The extension's socket reply to a focus command."""

    type: Literal["focus_result"]
    request_id: Identifier
    binding_id: Identifier
    status: FocusStatus
