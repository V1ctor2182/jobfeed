"""Ephemeral, explicitly paired connections that can focus an existing tab."""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from jobfeed.apply_health import EvidenceError, ReadBudget
from jobfeed.services.application_tab_models import (
    ApplicationProfileProof,
    ApplicationTabAutoConnectRequest,
    ApplicationTabBinding,
    ApplicationTabCandidate,
    ApplicationTabConnectRequest,
    ApplicationTabCredentials,
    ApplicationTabHello,
    ApplicationTabPairing,
    ApplicationTabReply,
    ApplicationTabsState,
    BindingState,
    FocusStatus,
)
from jobfeed.services.jobright_bridge import JobrightBridge, JobrightBridgeError

PAIRING_TTL = timedelta(minutes=5)
MEMORY_LIMIT = 100
IDENTIFIER_LIMIT = 256


class ApplicationTabError(Exception):
    """A tab pairing, binding or focus failure with an API error code and status."""

    def __init__(
        self, code: str, status: int = 409, *, message: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message or code.replace("_", " ")


def tab_origin(url: str) -> str:
    """Normalize an application tab URL to its scheme, host and non-default port.

    Args:
        url: Tab URL reported by the browser or the applier.

    Returns:
        The origin, with IPv6 hosts bracketed and default ports dropped.

    Raises:
        ApplicationTabError: If the URL has whitespace, control characters,
            backslashes, credentials, no host, or a scheme other than HTTP(S).
        ValueError: Internally, to route an unusable URL to the error above.
    """
    if "\\" in url or any(
        character.isspace() or not character.isprintable() for character in url
    ):
        raise ApplicationTabError("invalid_tab_url", 422)
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = (
            f":{port}"
            if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]
            else ""
        )
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        raise ApplicationTabError("invalid_tab_url", 422) from None


@dataclass(frozen=True)
class RecordedTab:
    """An application tab the applier recorded for one job."""

    scope: str
    record_id: str
    browser_id: str
    tab_id: str
    origin: str


@dataclass
class Pairing:
    """A short-lived pairing code waiting for the extension to connect a tab."""

    job_id: int
    owner: str
    expires_at: datetime
    expected_tab: tuple[int, str] | None = None
    browser_session: str | None = None


@dataclass
class ProfileProof:
    """Tabs the extension confirmed in its Chrome profile, valid until expiry."""

    connection: object
    browser_session: str
    expires_at: datetime
    matches: list[ApplicationTabCandidate]


@dataclass
class TabConnection:
    """Command queue and pending focus replies for one live extension socket."""

    commands: asyncio.Queue[dict[str, str | int]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=4)
    )
    pending: dict[str, asyncio.Future[FocusStatus]] = field(default_factory=dict)


@dataclass
class TabBinding:
    """A recorded application tab bound to the extension for one job."""

    job_id: int
    owner: str
    binding_id: str
    token: str
    browser_session: str
    extension_origin: str
    target: RecordedTab
    state: BindingState = "disconnected"
    connection: TabConnection | None = None


class ApplicationTabs:
    """In-memory pairing, binding and focus state for recorded application tabs."""

    def __init__(
        self, root: Path, db_path: Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self.root = root.resolve()
        self.db_path = db_path.resolve()
        self.now = now or (lambda: datetime.now(UTC))
        self.csrf_token = secrets.token_urlsafe(32)
        self.pairings: dict[str, Pairing] = {}
        self.bindings: dict[str, TabBinding] = {}
        self.focus_timeout = 5.0
        self.profile_proofs: dict[str, ProfileProof] = {}
        self.connecting_jobs: set[int] = set()

    def profile(self, token: str, bridge: JobrightBridge) -> ProfileProof:
        """Return a live profile proof for the bridge's current extension connection.

        Args:
            token: Profile proof token returned by `probe_profile`.
            bridge: Bridge whose extension connection the proof must belong to.

        Returns:
            The stored profile proof.

        Raises:
            ApplicationTabError: If the proof is missing or expired, or the extension
                reconnected since it was issued.
        """
        proof = self.profile_proofs.get(token)
        if proof is None or proof.expires_at <= self.now():
            raise ApplicationTabError(
                "profile_proof_expired",
                409,
                message=(
                    "Chrome profile verification expired. "
                    "Check the Jobfeed profile again."
                ),
            )
        if bridge.tab_connection is not proof.connection:
            raise ApplicationTabError(
                "profile_connection_changed",
                409,
                message=(
                    "The Jobfeed extension reconnected. "
                    "Verify its Chrome profile again."
                ),
            )
        return proof

    async def probe_profile(
        self, candidates: list[ApplicationTabCandidate], bridge: JobrightBridge
    ) -> ApplicationProfileProof:
        """Ask the extension which candidate tabs are open in its Chrome profile.

        Args:
            candidates: Tabs to look for; each URL must have a valid origin.
            bridge: Bridge used to send the probe to the extension.

        Returns:
            A short-lived proof token listing the candidates the extension found.

        Raises:
            ApplicationTabError: If the extension is unavailable or reconnects, its
                reply is malformed or names unrequested tabs, no candidate is in the
                profile, or too many proofs are outstanding.
        """
        for candidate in candidates:
            tab_origin(candidate.url)
        connection = bridge.tab_connection
        try:
            result = await bridge.tab_command(
                {
                    "action": "probe",
                    "candidates": [item.model_dump() for item in candidates],
                }
            )
        except JobrightBridgeError as exc:
            raise ApplicationTabError(
                "extension_profile_unavailable", 503, message=str(exc)
            ) from None
        if connection is None or bridge.tab_connection is not connection:
            raise ApplicationTabError("profile_connection_changed")
        session = result.get("browser_session_id")
        matches = result.get("matches")
        if (
            not isinstance(session, str)
            or not session
            or len(session) > IDENTIFIER_LIMIT
            or not isinstance(matches, list)
            or len(matches) > len(candidates)
        ):
            raise ApplicationTabError("invalid_profile_reply", 503)
        allowed = {(item.native_tab_id, item.url): item for item in candidates}
        verified = []
        for item in matches:
            if (
                not isinstance(item, dict)
                or type(item.get("native_tab_id")) is not int
                or not isinstance(item.get("url"), str)
                or (item["native_tab_id"], item["url"]) not in allowed
            ):
                raise ApplicationTabError("invalid_profile_reply", 503)
            verified.append(allowed[(item["native_tab_id"], item["url"])])
        if not verified:
            raise ApplicationTabError(
                "no_tabs_in_jobfeed_profile",
                message=(
                    "These tabs are not in the Chrome profile connected to Jobfeed. "
                    "Connect Codex browser control in that profile before rerunning."
                ),
            )
        self.profile_proofs = {
            key: value
            for key, value in self.profile_proofs.items()
            if value.expires_at > self.now() and value.connection is connection
        }
        if len(self.profile_proofs) >= MEMORY_LIMIT:
            raise ApplicationTabError("profile_proof_capacity", 429)
        token = secrets.token_urlsafe(24)
        expires = self.now() + PAIRING_TTL
        self.profile_proofs[token] = ProfileProof(
            connection, session, expires, verified
        )
        return ApplicationProfileProof(
            profile_proof=token, expires_at=expires.isoformat(), matches=verified
        )

    async def auto_connect(
        self,
        job_id: int,
        body: ApplicationTabAutoConnectRequest,
        bridge: JobrightBridge,
        base_url: str,
    ) -> ApplicationTabBinding:
        """Pair and bind one proven, recorded tab for a job through the extension.

        Args:
            job_id: Job whose recorded tab should be bound.
            body: Owner thread, profile proof and the tab to connect.
            bridge: Bridge used to send the connect command to the extension.
            base_url: Jobfeed base URL the extension should connect back to.

        Returns:
            The confirmed connected binding.

        Raises:
            ApplicationTabError: If the tab is not in the proof or not recorded, a
                connection is already pending for the job, or the extension does not
                confirm a matching connected binding.
        """
        proof = self.profile(body.profile_proof, bridge)
        if (
            ApplicationTabCandidate(native_tab_id=body.native_tab_id, url=body.url)
            not in proof.matches
        ):
            raise ApplicationTabError("selected_tab_not_in_profile_proof")
        origin = tab_origin(body.url)
        targets = [
            target
            for target in self._targets(job_id, body.owner_thread_id)
            if target.tab_id == str(body.native_tab_id) and target.origin == origin
        ]
        if len(targets) != 1:
            raise ApplicationTabError("selected_tab_not_recorded")
        if job_id in self.connecting_jobs:
            raise ApplicationTabError("tab_connection_pending")
        code = self.pairing(job_id, body.owner_thread_id).code
        self.connecting_jobs.add(job_id)
        self.pairings[code].expected_tab = (
            body.native_tab_id,
            body.url.split("#", 1)[0],
        )
        self.pairings[code].browser_session = proof.browser_session
        try:
            result = await bridge.tab_command(
                {
                    "action": "connect",
                    "code": code,
                    "base_url": base_url,
                    "native_tab_id": body.native_tab_id,
                    "url": body.url,
                    "browser_session_id": proof.browser_session,
                }
            )
            self.profile(body.profile_proof, bridge)
            binding_id = result.get("binding_id")
            binding = (
                self.bindings.get(binding_id) if isinstance(binding_id, str) else None
            )
            if (
                binding is None
                or binding.job_id != job_id
                or binding.owner != body.owner_thread_id
                or binding.target != targets[0]
                or binding.browser_session != proof.browser_session
            ):
                raise ApplicationTabError("tab_connection_unconfirmed", 503)
            self._validate(binding)
            if binding.state != "connected" or binding.connection is None:
                raise ApplicationTabError("tab_connection_unconfirmed", 503)
            self._remember(binding, body.url)
            return ApplicationTabBinding(
                job_id=job_id,
                owner_thread_id=binding.owner,
                binding_id=binding.binding_id,
                state=binding.state,
            )
        except JobrightBridgeError as exc:
            raise ApplicationTabError(
                "extension_pairing_unconfirmed", 503, message=str(exc)
            ) from None
        finally:
            self.pairings.pop(code, None)
            self.connecting_jobs.discard(job_id)

    def _registration_path(self) -> Path:
        path = self.root / ".pipeline" / "application-tabs.sqlite3"
        if path.resolve() != path:
            raise ApplicationTabError("tab_registration_unavailable", 503)
        return path

    def _remember(self, binding: TabBinding, url: str) -> None:
        target = binding.target
        try:
            with closing(sqlite3.connect(self._registration_path(), timeout=0.2)) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS registered_tabs ("
                    "job_id INTEGER PRIMARY KEY, owner TEXT, scope TEXT, "
                    "record_id TEXT, browser_id TEXT, tab_id TEXT, origin TEXT, "
                    "url TEXT, browser_session TEXT)"
                )
                db.execute(
                    "INSERT OR REPLACE INTO registered_tabs VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        binding.job_id,
                        binding.owner,
                        target.scope,
                        target.record_id,
                        target.browser_id,
                        target.tab_id,
                        target.origin,
                        url,
                        binding.browser_session,
                    ),
                )
                db.commit()
        except sqlite3.Error:
            raise ApplicationTabError("tab_registration_unavailable", 503) from None

    def _registered(
        self, job_id: int, owner: str, targets: list[RecordedTab]
    ) -> tuple[RecordedTab, ApplicationTabCandidate, str]:
        try:
            with closing(
                sqlite3.connect(
                    self._registration_path().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=0.2,
                )
            ) as db:
                row = db.execute(
                    "SELECT scope,record_id,browser_id,tab_id,origin,"
                    "url,browser_session "
                    "FROM registered_tabs WHERE job_id=? AND owner=?",
                    (job_id, owner),
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row is None or RecordedTab(*row[:5]) not in targets:
            raise ApplicationTabError(
                "tab_registration_missing",
                message="The applier has not connected this application form yet.",
            )
        target = RecordedTab(*row[:5])
        try:
            candidate = ApplicationTabCandidate(
                native_tab_id=int(target.tab_id), url=row[5]
            )
            if (
                str(candidate.native_tab_id) != target.tab_id
                or tab_origin(candidate.url) != target.origin
            ):
                raise ValueError
            if not isinstance(row[6], str) or not 0 < len(row[6]) <= IDENTIFIER_LIMIT:
                raise ValueError
        except (ValueError, TypeError):
            raise ApplicationTabError("tab_registration_unavailable", 503) from None
        return target, candidate, row[6]

    async def open_existing(
        self, job_id: int, owner: str, bridge: JobrightBridge, base_url: str
    ) -> FocusStatus:
        """Focus a job's registered tab, reconnecting it first when no binding is live.

        Args:
            job_id: Job whose registered tab should be opened.
            owner: Owner thread ID the tab was recorded for.
            bridge: Bridge used to probe and connect through the extension.
            base_url: Jobfeed base URL the extension should connect back to.

        Returns:
            The focus status reported by the extension.

        Raises:
            ApplicationTabError: If the connected Chrome profile changed or the
                recorded tab changed during reconnection.
        """
        targets = self._targets(job_id, owner)
        for binding in self.bindings.values():
            if (
                binding.job_id == job_id
                and binding.owner == owner
                and binding.target in targets
                and binding.state == "connected"
                and binding.connection is not None
            ):
                return await self.focus(job_id, owner, binding.binding_id)
        target, candidate, session = self._registered(job_id, owner, targets)
        proof = await self.probe_profile([candidate], bridge)
        if self.profile(proof.profile_proof, bridge).browser_session != session:
            raise ApplicationTabError(
                "registered_profile_changed",
                message=(
                    "The connected Chrome profile has changed. "
                    "The applier must reconnect its existing form."
                ),
            )
        if target not in self._targets(job_id, owner):
            raise ApplicationTabError("tab_binding_changed")
        reconnected = await self.auto_connect(
            job_id,
            ApplicationTabAutoConnectRequest(
                owner_thread_id=owner,
                profile_proof=proof.profile_proof,
                native_tab_id=candidate.native_tab_id,
                url=candidate.url,
            ),
            bridge,
            base_url,
        )
        return await self.focus(job_id, owner, reconnected.binding_id)

    def _assert_current_owner(self, job_id: int, owner: str) -> None:
        with closing(
            sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as db:
            current = db.execute(
                "SELECT codex_application_session_id FROM application_queue "
                "WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if current is not None:
                if current[0] != owner:
                    raise ApplicationTabError("owner_changed")
                return
            owners = set()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='application_terminal'"
            ).fetchone():
                terminal = db.execute(
                    "SELECT codex_application_session_id FROM application_terminal "
                    "WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if terminal is not None:
                    owners.add(terminal[0])
        budget = ReadBudget(self.root, remaining=65536)
        for directory in ("submission-closeouts", "closed-jobs"):
            path = self.root / ".pipeline" / directory / f"{job_id}.json"
            if path.exists():
                record = budget.object(path)
                if type(record.get("job_id")) is not int or record["job_id"] != job_id:
                    raise ApplicationTabError("owner_changed")
                if not isinstance(record.get("thread_id"), str):
                    raise ApplicationTabError("owner_changed")
                owners.add(record["thread_id"])
        if owners != {owner}:
            raise ApplicationTabError("owner_changed")

    def _targets(self, job_id: int, owner: str) -> list[RecordedTab]:
        try:
            self._assert_current_owner(job_id, owner)
            gate_path = self.root / ".pipeline" / "execution-gates.sqlite3"
            with closing(
                sqlite3.connect(gate_path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
            ) as db:
                rows = db.execute(
                    "SELECT scope FROM execution_bindings "
                    "WHERE job_id=? AND owner=? LIMIT 2",
                    (job_id, owner),
                ).fetchall()
            if len(rows) != 1:
                raise ApplicationTabError("application_scope_unavailable")
            scope = rows[0][0]
            manifest = Path(scope).resolve()
            if not manifest.is_relative_to(self.root / ".pipeline" / "batches"):
                raise ApplicationTabError("application_scope_unavailable")
            data = ReadBudget(self.root).object(manifest)
            candidates = data.get("ordered_candidates")
            if not isinstance(candidates, list) or not any(
                isinstance(job, dict)
                and type(job.get("id")) is int
                and job["id"] == job_id
                for job in candidates
            ):
                raise ApplicationTabError("job_missing_from_scope")
            path = self.root / ".pipeline" / "coordination.sqlite3"
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
            ) as db:
                records = db.execute(
                    "SELECT t.id,t.browser_id,t.tab_id,t.origin "
                    "FROM coordination_tabs t JOIN coordination_bindings b "
                    "ON b.scope=t.scope AND b.job_id=t.job_id AND b.owner=t.owner "
                    "WHERE t.scope=? AND t.job_id=? AND t.owner=? "
                    "AND t.state IN ('observed','preserved') LIMIT 101",
                    (scope, job_id, owner),
                ).fetchall()
            if len(records) > MEMORY_LIMIT:
                raise ApplicationTabError("too_many_recorded_tabs")
            return [RecordedTab(scope, *record) for record in records]
        except (sqlite3.Error, OSError, ValueError, EvidenceError):
            raise ApplicationTabError("tab_evidence_unavailable", 503) from None

    def _validate(self, binding: TabBinding) -> None:
        if binding.target not in self._targets(binding.job_id, binding.owner):
            binding.state = "tab_closed"
            raise ApplicationTabError("tab_binding_revoked")

    def state(self) -> ApplicationTabsState:
        """Report the CSRF token and every current tab binding.

        Returns:
            The tab state exposed to the dashboard.
        """
        return ApplicationTabsState(
            csrf_token=self.csrf_token,
            bindings=[
                ApplicationTabBinding(
                    job_id=b.job_id,
                    owner_thread_id=b.owner,
                    binding_id=b.binding_id,
                    state=b.state,
                )
                for b in self.bindings.values()
            ],
        )

    def pairing(self, job_id: int, owner: str) -> ApplicationTabPairing:
        """Issue a pairing code for a job, replacing the job's earlier codes.

        Args:
            job_id: Job whose recorded tab will be paired.
            owner: Owner thread ID the tab was recorded for.

        Returns:
            The new pairing code and its expiry time.

        Raises:
            ApplicationTabError: If the job has no recorded tab or too many pairings
                are outstanding.
        """
        if not self._targets(job_id, owner):
            raise ApplicationTabError("no_recorded_application_tab")
        now = self.now()
        self.pairings = {
            code: pair
            for code, pair in self.pairings.items()
            if pair.expires_at > now and pair.job_id != job_id
        }
        if len(self.pairings) >= MEMORY_LIMIT:
            raise ApplicationTabError("pairing_capacity", 429)
        code = secrets.token_urlsafe(24)
        expires = now + PAIRING_TTL
        self.pairings[code] = Pairing(job_id, owner, expires)
        return ApplicationTabPairing(code=code, expires_at=expires.isoformat())

    def connect(
        self, body: ApplicationTabConnectRequest, extension_origin: str
    ) -> ApplicationTabCredentials:
        """Redeem a pairing code and bind the extension's tab, revoking older bindings.

        Args:
            body: Pairing code, selected tab and browser session from the extension.
            extension_origin: Origin of the extension making the request.

        Returns:
            The new binding ID and its session token.

        Raises:
            ApplicationTabError: If the code is expired or used, the tab or browser
                session differs from what was paired, the tab is not recorded, or too
                many bindings exist.
        """
        pair = self.pairings.pop(body.code, None)
        if pair is None or pair.expires_at <= self.now():
            raise ApplicationTabError("pairing_expired_or_used", 403)
        origin = tab_origin(body.url)
        if pair.expected_tab is not None and pair.expected_tab != (
            body.native_tab_id,
            body.url,
        ):
            raise ApplicationTabError("selected_tab_url_changed")
        if (
            pair.browser_session is not None
            and pair.browser_session != body.browser_session_id
        ):
            raise ApplicationTabError("profile_session_changed")
        targets = [
            target
            for target in self._targets(pair.job_id, pair.owner)
            if target.tab_id == str(body.native_tab_id) and target.origin == origin
        ]
        if len(targets) != 1:
            raise ApplicationTabError("selected_tab_not_recorded")
        old = [b for b in self.bindings.values() if b.job_id == pair.job_id]
        if not old and len(self.bindings) >= MEMORY_LIMIT:
            raise ApplicationTabError("binding_capacity", 429)
        for binding in old:
            self._revoke(binding)
            del self.bindings[binding.binding_id]
        binding_id, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        self.bindings[binding_id] = TabBinding(
            pair.job_id,
            pair.owner,
            binding_id,
            token,
            body.browser_session_id,
            extension_origin,
            targets[0],
        )
        return ApplicationTabCredentials(binding_id=binding_id, session_token=token)

    def attach(
        self, hello: ApplicationTabHello, extension_origin: str
    ) -> TabConnection:
        """Attach an extension socket to a binding after checking its credentials.

        Args:
            hello: Binding ID, session token and browser session from the extension.
            extension_origin: Origin of the extension opening the socket.

        Returns:
            The new connection that carries commands for the binding.

        Raises:
            ApplicationTabError: If the credentials do not match, the tab is closed
                or changed, or the binding is already connected.
        """
        binding = self.bindings.get(hello.binding_id)
        if (
            binding is None
            or not secrets.compare_digest(binding.token, hello.session_token)
            or binding.extension_origin != extension_origin
            or binding.browser_session != hello.browser_session_id
        ):
            raise ApplicationTabError("invalid_tab_session", 403)
        self._validate(binding)
        if binding.state in {"tab_closed", "page_changed"}:
            raise ApplicationTabError(binding.state)
        if binding.connection is not None:
            raise ApplicationTabError("tab_already_connected")
        connection = TabConnection()
        binding.connection = connection
        binding.state = "connected"
        return connection

    def _revoke(self, binding: TabBinding) -> None:
        connection = binding.connection
        if connection is not None:
            self.detach(binding.binding_id, connection)
            with suppress(asyncio.QueueFull):
                connection.commands.put_nowait({"type": "revoked"})

    def detach(self, binding_id: str, connection: TabConnection) -> None:
        """Disconnect a binding's socket and fail its pending focus requests.

        Args:
            binding_id: Binding whose socket closed.
            connection: The socket's connection; ignored if no longer current.
        """
        binding = self.bindings.get(binding_id)
        if binding is None or binding.connection is not connection:
            return
        binding.connection = None
        if binding.state == "connected":
            binding.state = "disconnected"
        for future in connection.pending.values():
            if not future.done():
                future.set_exception(ApplicationTabError("tab_disconnected", 503))
        connection.pending.clear()

    async def focus(self, job_id: int, owner: str, binding_id: str) -> FocusStatus:
        """Ask the extension to focus a bound tab and wait for its reply.

        Args:
            job_id: Job the binding must belong to.
            owner: Owner thread ID the binding must belong to.
            binding_id: Binding whose tab should be focused.

        Returns:
            The focus status reported by the extension.

        Raises:
            ApplicationTabError: If the binding is unknown, invalid, disconnected,
                closed, already focusing, or the reply does not arrive in time.
        """
        binding = self.bindings.get(binding_id)
        if binding is None or binding.job_id != job_id or binding.owner != owner:
            raise ApplicationTabError("binding_not_found", 404)
        try:
            self._validate(binding)
        except ApplicationTabError:
            self._revoke(binding)
            raise
        connection = binding.connection
        if connection is None:
            raise ApplicationTabError("tab_disconnected", 503)
        if binding.state in {"tab_closed", "page_changed"}:
            raise ApplicationTabError(binding.state)
        if connection.pending:
            raise ApplicationTabError("focus_already_pending")
        request_id = secrets.token_urlsafe(24)
        future: asyncio.Future[FocusStatus] = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = future
        try:
            connection.commands.put_nowait(
                {
                    "type": "focus",
                    "request_id": request_id,
                    "binding_id": binding_id,
                    "expires_at": int(
                        (self.now() + timedelta(seconds=self.focus_timeout)).timestamp()
                        * 1000
                    ),
                }
            )
            status = await asyncio.wait_for(future, timeout=self.focus_timeout)
            self._validate(binding)
            return status
        except (TimeoutError, asyncio.QueueFull):
            raise ApplicationTabError(
                "focus_timeout_outcome_unconfirmed", 504
            ) from None
        finally:
            connection.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def receive(self, reply: ApplicationTabReply, connection: TabConnection) -> None:
        """Resolve a pending focus request with the extension's reply.

        Args:
            reply: Focus reply from the extension.
            connection: Connection the reply arrived on; ignored if not current.
        """
        binding = self.bindings.get(reply.binding_id)
        if binding is None or binding.connection is not connection:
            return
        future = connection.pending.get(reply.request_id)
        if future is None or future.done():
            return
        if reply.status in {"tab_closed", "page_changed"}:
            binding.state = reply.status
        future.set_result(reply.status)
