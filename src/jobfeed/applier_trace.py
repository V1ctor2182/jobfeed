"""Claude Applier runs as trace trees, read from what the pipeline already records.

Read only. The pipeline's Applier runtime store gives the launch, the user
requests and the Submit attempt; each Claude session transcript gives the tool
calls. A step exposes a tool's name and at most one allowlisted input detail,
never tool results or form values.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from jobfeed.services.applier_trace_models import (
    ApplierRun,
    ApplierTrace,
    StepCategory,
    TraceGroup,
    TraceStep,
)

ENDED_STATES = frozenset({"finished", "failed", "stopped"})
WAITING_REQUEST_STATES = frozenset({"open", "answered", "redrafted"})
SESSION_GRACE = timedelta(minutes=1)
NOTE_LIMIT = 200
DETAIL_LIMIT = 80
FIRST_TIMESTAMP_LINES = 200
# record_progress takes free text; only the pipeline's own step names are shown.
LIFECYCLE_STEPS = frozenset({"ready for review", "submitted"})

Detail = Callable[[dict[str, object]], str | None]


def _no_detail(_input: dict[str, object]) -> None:
    return None


def _text(name: str) -> Detail:
    def read(tool_input: dict[str, object]) -> str | None:
        value = tool_input.get(name)
        return value[:DETAIL_LIMIT] if isinstance(value, str) and value else None

    return read


def _choice(name: str, allowed: frozenset[str]) -> Detail:
    """A free-text input shown only when it is one of the known values."""

    def read(tool_input: dict[str, object]) -> str | None:
        value = tool_input.get(name)
        return value if value in allowed else None

    return read


def _host(tool_input: dict[str, object]) -> str | None:
    url = tool_input.get("url")
    try:
        return urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return None


def _basename(tool_input: dict[str, object]) -> str | None:
    path = tool_input.get("file_path")
    return PurePath(path).name[:DETAIL_LIMIT] if isinstance(path, str) else None


@dataclass(frozen=True)
class Tool:
    """Registry entry describing how one Applier tool call is shown in a trace."""

    label: str
    category: StepCategory
    detail: Detail = _no_detail
    fills_field: bool = False


CHROME = "mcp__claude-in-chrome__"
APPLIER = "mcp__applier__"
NAMESPACE_CATEGORIES: dict[str, StepCategory] = {CHROME: "browser", APPLIER: "backend"}
TOOLS: dict[str, Tool] = {
    CHROME + "tabs_context_mcp": Tool("Check tab", "browser"),
    CHROME + "navigate": Tool("Open page", "browser", _host),
    CHROME + "form_input": Tool("Fill field", "browser", fills_field=True),
    CHROME + "file_upload": Tool("Upload file", "browser", fills_field=True),
    CHROME + "computer": Tool("Click or type", "browser", _text("action")),
    CHROME + "find": Tool("Find element", "browser"),
    CHROME + "read_page": Tool("Read page", "browser"),
    CHROME + "get_page_text": Tool("Read page", "browser"),
    CHROME + "javascript_tool": Tool("Run script", "browser"),
    APPLIER + "register_tab": Tool("Register tab", "backend"),
    APPLIER + "record_progress": Tool(
        "Record progress", "backend", _choice("step", LIFECYCLE_STEPS)
    ),
    APPLIER + "lookup_answer": Tool("Look up answer", "backend"),
    APPLIER + "lookup_profile": Tool("Read profile", "backend"),
    APPLIER + "get_draft_feedback": Tool("Read draft feedback", "backend"),
    APPLIER + "list_resumes": Tool("List résumés", "backend"),
    APPLIER + "select_resume": Tool("Pick résumé", "backend"),
    APPLIER + "get_notes": Tool("Read site notes", "backend"),
    APPLIER + "get_helper": Tool("Load helper", "backend"),
    APPLIER + "save_note": Tool("Save note", "backend"),
    APPLIER + "request_submit": Tool("Request Submit", "backend"),
    APPLIER + "record_readback": Tool("Record read-back", "backend"),
    APPLIER + "record_confirmation": Tool("Record confirmation", "backend"),
    APPLIER + "record_submit_outcome": Tool("Record Submit outcome", "backend"),
    APPLIER + "ask_user": Tool("Ask you", "human"),
    APPLIER + "report_blocker": Tool("Report blocker", "human", _text("blocker_type")),
    "Read": Tool("Read file", "shell", _basename),
    "ToolSearch": Tool("Load tools", "shell"),
    "Glob": Tool("Find files", "shell"),
    "Grep": Tool("Search files", "shell"),
}


def tool_for(name: str) -> tuple[str, Tool]:
    """Return a tool's short name and its registry entry, or a raw fallback.

    Args:
        name: Full tool name as recorded in the transcript, including any namespace.

    Returns:
        The name without its namespace prefix and the matching or fallback `Tool`.
    """
    for prefix, category in NAMESPACE_CATEGORIES.items():
        if name.startswith(prefix):
            short = name.removeprefix(prefix)
            return short, TOOLS.get(name, Tool(short, category))
    return name, TOOLS.get(name, Tool(name, "model"))


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 transcript timestamp, assuming UTC when no zone is given.

    Args:
        value: Raw timestamp value from a transcript record.

    Returns:
        The aware datetime, or None when the value is not a parseable string.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class ToolCall:
    """One tool invocation observed in a Claude transcript."""

    id: str
    name: str
    detail: str | None
    start: datetime
    end: datetime | None = None
    failed: bool = False


@dataclass(frozen=True)
class Session:
    """Tool calls and time bounds of one Claude transcript."""

    id: str
    started: datetime
    last: datetime
    calls: list[ToolCall]


def _records(lines: Iterable[str]) -> Iterator[tuple[dict[str, object], datetime]]:
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            at = parse_timestamp(record.get("timestamp"))
            if at is not None:
                yield record, at


def _blocks(record: dict[str, object]) -> list[dict[str, object]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _observe(
    calls: dict[str, ToolCall], record: dict[str, object], at: datetime
) -> None:
    for block in _blocks(record):
        if record.get("type") == "assistant" and block.get("type") == "tool_use":
            call_id, name, tool_input = (
                block.get("id"),
                block.get("name"),
                block.get("input"),
            )
            if isinstance(call_id, str) and isinstance(name, str):
                detail = tool_for(name)[1].detail(
                    tool_input if isinstance(tool_input, dict) else {}
                )
                calls.setdefault(call_id, ToolCall(call_id, name, detail, at))
        elif record.get("type") == "user" and block.get("type") == "tool_result":
            call = calls.get(str(block.get("tool_use_id")))
            if call is not None and call.end is None:
                call.end, call.failed = at, block.get("is_error") is True


def parse_session(session_id: str, lines: Iterable[str]) -> Session | None:
    """Collect the tool calls of one Claude transcript.

    Args:
        session_id: Transcript session ID, used as the session's ID.
        lines: Raw JSONL lines of the transcript.

    Returns:
        The session with calls sorted by start time, or None when no line carries a
        timestamp.
    """
    calls: dict[str, ToolCall] = {}
    first: datetime | None = None
    last: datetime | None = None
    for record, at in _records(lines):
        first = first or at
        last = at if last is None else max(last, at)
        _observe(calls, record, at)
    if first is None or last is None:
        return None
    return Session(
        session_id, first, last, sorted(calls.values(), key=lambda c: c.start)
    )


@dataclass(frozen=True)
class Request:
    """A user request an Applier raised during a recorded launch."""

    blocker_type: str | None
    field: str | None
    text: str
    state: str
    created_at: datetime
    replied_at: datetime | None


@dataclass(frozen=True)
class Submit:
    """The submit attempt recorded for a launch."""

    state: str
    attempts: int
    requested_at: datetime
    outcome_at: datetime | None


@dataclass(frozen=True)
class Launch:
    """One recorded Applier launch with its requests and submit attempt."""

    job_id: int
    name: str
    state: str
    cwd: str | None
    session_id: str | None
    created_at: datetime
    updated_at: datetime
    job_created_at: datetime | None
    model: str | None
    requests: list[Request] = field(default_factory=list)
    submit: Submit | None = None

    @property
    def is_live(self) -> bool:
        """Whether the launch has not reached an ended state.

        Returns:
            True while the launch state is not in `ENDED_STATES`.
        """
        return self.state not in ENDED_STATES

    @property
    def started_at(self) -> datetime:
        """When the run began, counting from the job's creation if that came first.

        Returns:
            The earlier of the launch and job creation times.
        """
        return min(self.created_at, self.job_created_at or self.created_at)


@dataclass(frozen=True)
class JobLabel:
    """Company and title shown for a job in a trace."""

    company: str | None
    title: str | None


class _Clock:
    def __init__(self, origin: datetime) -> None:
        self.origin = origin

    def at(self, moment: datetime) -> float:
        return round((moment - self.origin).total_seconds(), 3)


def _step_meta(short: str, calls: list[ToolCall]) -> str:
    meta = short if len(calls) == 1 else f"{short} × {len(calls)}"  # noqa: RUF001
    details = {call.detail for call in calls}
    detail = details.pop() if len(details) == 1 else None
    return f"{meta} · {detail}" if detail else meta


def _tool_step(calls: list[ToolCall], clock: _Clock, horizon: datetime) -> TraceStep:
    first = calls[0]
    short, tool = tool_for(first.name)
    done = [call for call in calls if call.end is not None and not call.failed]
    return TraceStep(
        kind="step",
        id=first.id,
        name=tool.label,
        meta=_step_meta(short, calls),
        category=tool.category,
        start=clock.at(first.start),
        end=clock.at(max(call.end or horizon for call in calls)),
        outcome="failed" if first.failed else "ok",
        fieldsFilled=len(done) if tool.fills_field else None,
        note="The tool returned an error." if first.failed else None,
    )


def _runs(calls: list[ToolCall]) -> list[list[ToolCall]]:
    """Consecutive successful calls of one tool merge; a failed call stands alone."""
    runs: list[list[ToolCall]] = []
    for call in calls:
        previous = runs[-1] if runs else None
        if (
            previous is not None
            and previous[0].name == call.name
            and tool_for(call.name)[1].category != "human"
            and not previous[0].failed
            and not call.failed
        ):
            previous.append(call)
        else:
            runs.append([call])
    return runs


def _applier_group(
    session: Session, model: str | None, clock: _Clock, horizon: datetime
) -> TraceGroup:
    steps: list[TraceGroup | TraceStep] = [
        _tool_step(run, clock, horizon) for run in _runs(session.calls)
    ]
    meta = f"Claude CLI · {session.id[:8]}" + (f" · {model}" if model else "")
    return TraceGroup(
        kind="group",
        id=f"applier-{session.id}",
        name="Applier",
        meta=meta,
        role="applier",
        start=clock.at(session.started),
        end=max([clock.at(horizon), *(step.end for step in steps)]),
        children=steps,
    )


def _request_step(
    request: Request, index: int, clock: _Clock, horizon: datetime
) -> TraceStep:
    end = request.replied_at or (
        horizon if request.state == "open" else request.created_at
    )
    note = request.text
    if len(note) > NOTE_LIMIT:
        note = note[: NOTE_LIMIT - 1].rstrip() + "…"
    return TraceStep(
        kind="step",
        id=f"request-{index}",
        name="Ask you",
        meta=" · ".join(part for part in (request.blocker_type, request.field) if part)
        or "question",
        category="human",
        start=clock.at(request.created_at),
        end=clock.at(max(end, request.created_at)),
        outcome="ok",
        note=note,
    )


def _submit_step(submit: Submit, clock: _Clock, horizon: datetime) -> TraceStep:
    failed = submit.outcome_at is not None and submit.state != "submitted"
    return TraceStep(
        kind="step",
        id="submit",
        name="Submit",
        meta=f"{submit.state} · attempt {submit.attempts}",
        category="backend",
        start=clock.at(submit.requested_at),
        end=clock.at(submit.outcome_at or horizon),
        outcome="failed" if failed else "ok",
    )


def _place(
    step: TraceStep, appliers: list[TraceGroup], worker: list[TraceStep]
) -> None:
    """Put a recorded step inside the Applier session it happened in.

    A user request replaces the ask_user or report_blocker call that raised it,
    so one wait is not drawn or counted twice.
    """
    home = next((g for g in appliers if g.start <= step.start <= g.end), None)
    if home is None:
        worker.append(step)
        return
    if step.category == "human":
        home.children = [
            child
            for child in home.children
            if not (
                child.kind == "step"
                and child.category == "human"
                and child.start <= step.start <= child.end
            )
        ]
    home.children.append(step)


def _subtitle(launch: Launch, label: JobLabel) -> str:
    job = (
        f"{label.title} at {label.company}"
        if label.title and label.company
        else label.title or label.company or f"Job #{launch.job_id}"
    )
    return f"{job} · Claude Applier · {launch.state}"


def build_trace(
    launch: Launch, sessions: list[Session], label: JobLabel, now: datetime
) -> ApplierTrace:
    """Assemble one run's tree. Open spans end now while the run is live.

    Args:
        launch: Recorded launch the trace describes.
        sessions: Parsed transcripts of the launch, oldest first.
        label: Company and title shown for the job.
        now: Current time, used as the end of open spans while the run is live.

    Returns:
        The run's trace with Applier groups and recorded request and submit steps.
    """
    clock = _Clock(launch.started_at)
    last_seen = max([launch.updated_at, *(s.last for s in sessions)])
    horizon = now if launch.is_live else last_seen
    appliers = [
        _applier_group(
            session,
            launch.model,
            clock,
            horizon if session is sessions[-1] else session.last,
        )
        for session in sessions
    ]
    recorded = [
        _request_step(request, index, clock, horizon)
        for index, request in enumerate(launch.requests)
        if request.state in WAITING_REQUEST_STATES
    ]
    if launch.submit is not None:
        recorded.append(_submit_step(launch.submit, clock, horizon))
    loose: list[TraceStep] = []
    for step in recorded:
        _place(step, appliers, loose)
    for group in appliers:
        group.children.sort(key=lambda child: child.start)
        group.end = max([group.end, *(child.end for child in group.children)])
    launch_step = TraceStep(
        kind="step",
        id="launch",
        name="Launch Applier",
        meta=f"claude --bg · {launch.name}",
        category="shell",
        start=clock.at(launch.created_at),
        end=clock.at(sessions[0].started if sessions else horizon),
        outcome="ok",
    )
    children: list[TraceGroup | TraceStep] = [launch_step, *appliers, *loose]
    children.sort(key=lambda child: child.start)
    return ApplierTrace(
        job_id=launch.job_id,
        started_at=launch.started_at.isoformat(),
        is_live=launch.is_live,
        title=f"Job #{launch.job_id}",
        subtitle=_subtitle(launch, label),
        fieldsTotal=None,
        root=TraceGroup(
            kind="group",
            id="worker",
            name="Worker",
            meta=f"job #{launch.job_id}",
            role="worker",
            start=0,
            end=max([clock.at(horizon), *(child.end for child in children)]),
            children=children,
        ),
    )


def run_summary(launch: Launch, label: JobLabel, session_count: int) -> ApplierRun:
    """Summarize a launch as one row of the runs list.

    Args:
        launch: Recorded launch to summarize.
        label: Company and title shown for the job.
        session_count: Number of transcripts found for the launch.

    Returns:
        The run summary with ISO-8601 timestamps.
    """
    return ApplierRun(
        job_id=launch.job_id,
        company=label.company,
        title=label.title,
        state=launch.state,
        started_at=launch.started_at.isoformat(),
        updated_at=launch.updated_at.isoformat(),
        is_live=launch.is_live,
        session_count=session_count,
    )


def encode_project_dir(cwd: str) -> str:
    """Claude Code's project folder name for a working directory.

    Args:
        cwd: Working directory of the Claude session.

    Returns:
        The path with every non-alphanumeric character replaced by a hyphen.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def default_projects_dir() -> Path:
    """Locate Claude Code's projects folder, honoring `CLAUDE_CONFIG_DIR`.

    Returns:
        The `projects` folder under the configured or default Claude directory.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def _read_only(path: Path) -> sqlite3.Connection:
    """Open the pipeline's runtime store read-only, after any crashed write is undone.

    A writer that died mid-write leaves a journal that a read-only connection
    cannot roll back, so every read fails. A normal connection rolls it back
    first; while a live writer still holds the file, it only reads.
    """
    if path.with_name(path.name + "-journal").exists():
        with closing(sqlite3.connect(path, timeout=10)) as db:
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def _rows(
    db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


_LAUNCHES = (
    "SELECT l.job_id, l.name, l.state, l.config, l.session_id, l.created_at, "
    "l.updated_at FROM applier_launches l"
)


def _cwd(config: object) -> str | None:
    try:
        value = json.loads(config) if isinstance(config, str) else None
    except ValueError:
        return None
    cwd = value.get("cwd") if isinstance(value, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def _requests(db: sqlite3.Connection, job_id: int) -> list[Request]:
    replies = {
        row["request_id"]: parse_timestamp(row["received_at"])
        for row in _rows(
            db,
            "SELECT request_id, MIN(received_at) AS received_at FROM applier_replies "
            "WHERE request_id IN (SELECT id FROM applier_requests WHERE job_id=?) "
            "GROUP BY request_id",
            (job_id,),
        )
    }
    requests = []
    for row in _rows(
        db,
        "SELECT id, blocker_type, field, text, state, created_at FROM "
        "applier_requests WHERE job_id=? ORDER BY created_at",
        (job_id,),
    ):
        created = parse_timestamp(row["created_at"])
        if created is not None:
            requests.append(
                Request(
                    row["blocker_type"],
                    row["field"],
                    row["text"],
                    row["state"],
                    created,
                    replies.get(row["id"]),
                )
            )
    return requests


def _submit(db: sqlite3.Connection, job_id: int) -> Submit | None:
    rows = _rows(
        db,
        "SELECT state, attempts, requested_at, outcome_at FROM applier_submits "
        "WHERE job_id=?",
        (job_id,),
    )
    requested = parse_timestamp(rows[0]["requested_at"]) if rows else None
    if requested is None:
        return None
    return Submit(
        rows[0]["state"],
        rows[0]["attempts"],
        requested,
        parse_timestamp(rows[0]["outcome_at"]),
    )


def _launch(db: sqlite3.Connection, row: sqlite3.Row) -> Launch | None:
    created, updated = (
        parse_timestamp(row["created_at"]),
        parse_timestamp(row["updated_at"]),
    )
    if created is None or updated is None:
        return None
    job = _rows(
        db,
        "SELECT model, created_at FROM applier_jobs WHERE job_id=?",
        (row["job_id"],),
    )
    return Launch(
        job_id=row["job_id"],
        name=row["name"],
        state=row["state"],
        cwd=_cwd(row["config"]),
        session_id=row["session_id"],
        created_at=created,
        updated_at=updated,
        job_created_at=parse_timestamp(job[0]["created_at"]) if job else None,
        model=job[0]["model"] if job else None,
        requests=_requests(db, row["job_id"]),
        submit=_submit(db, row["job_id"]),
    )


def _first_timestamp(path: Path) -> datetime | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            head = (
                line
                for _, line in zip(range(FIRST_TIMESTAMP_LINES), lines, strict=False)
            )
            return next((at for _, at in _records(head)), None)
    except OSError:
        return None


class ApplierTraceReader:
    """Reads Applier runs without creating or changing any pipeline file."""

    def __init__(
        self,
        root: Path,
        projects_dir: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = root / ".pipeline" / "applier-runtime.sqlite3"
        self.projects_dir = projects_dir or default_projects_dir()
        self.now = now or (lambda: datetime.now(UTC))

    def launches(self, job_id: int | None = None) -> list[Launch]:
        """Recorded launches, newest first; none when the store does not exist.

        Args:
            job_id: Restrict the result to this job, or None for every job.

        Returns:
            Launches sorted by creation time, newest first.
        """
        if not self.db_path.is_file():
            return []
        where = (" WHERE l.job_id=?", (job_id,)) if job_id is not None else ("", ())
        with closing(_read_only(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            rows = _rows(db, _LAUNCHES + where[0], where[1])
            launches = [launch for row in rows if (launch := _launch(db, row))]
        return sorted(launches, key=lambda launch: launch.created_at, reverse=True)

    def autopilot_enabled(self) -> bool:
        """Whether the user's autopilot switch is on.

        Returns:
            True only when the store holds a readable record that says so.
        """
        if not self.db_path.is_file():
            return False
        with closing(_read_only(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            rows = _rows(db, "SELECT value FROM applier_control WHERE key='autopilot'")
        try:
            record = json.loads(rows[0]["value"]) if rows else None
        except ValueError:
            return False
        return isinstance(record, dict) and record.get("enabled") is True

    def transcripts(self, launch: Launch) -> list[Path]:
        """The launch's session transcripts, oldest first.

        The recorded session counts, and so does every other session in the
        launch's folder that began after the launch, such as one after a restart.

        Args:
            launch: Launch whose working directory holds the transcripts.

        Returns:
            Transcript paths ordered by their first timestamp.
        """
        if launch.cwd is None:
            return []
        directory = self.projects_dir / encode_project_dir(launch.cwd)
        found = []
        for path in directory.glob("*.jsonl"):
            first = _first_timestamp(path)
            if first is not None and (
                path.stem == launch.session_id
                or first >= launch.created_at - SESSION_GRACE
            ):
                found.append((first, path))
        return [path for _, path in sorted(found)]

    def sessions(self, launch: Launch) -> list[Session]:
        """Parse the launch's transcripts, skipping unreadable or empty ones.

        Args:
            launch: Launch whose transcripts should be parsed.

        Returns:
            Parsed sessions, oldest first.
        """
        sessions = []
        for path in self.transcripts(launch):
            try:
                with path.open(encoding="utf-8", errors="replace") as lines:
                    session = parse_session(path.stem, lines)
            except OSError:
                continue
            if session is not None:
                sessions.append(session)
        return sessions
