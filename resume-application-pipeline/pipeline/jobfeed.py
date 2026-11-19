"""Read Jobfeed jobs and delegate successful-application writes to its CLI service."""

from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import urlopen
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


# The Jobfeed dashboard serves this API on 127.0.0.1:8765.
DEFAULT_API_URL = "http://127.0.0.1:8765"


def inspection_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('inspection URL must be a bounded HTTPS URL')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError('inspection URL must use HTTPS without credentials or fragments')
    _ = parsed.port
    return value


def excluded_company(company: str) -> bool:
    from jobfeed.domain.company_policy import excluded_brand
    return excluded_brand(company)


@dataclass(frozen=True)
class Job:
    id: int
    company: str
    title: str
    description: str
    url: str


class Jobfeed:
    def __init__(self, db_path: Path, repo_root: Path, *, api_url=None, transport=None):
        self.db_path = db_path
        self.repo_root = repo_root
        self.api_url = api_url
        self.transport = transport

    def _canonical_page(self, offset: int) -> dict:
        # Reuse the public Results view: current policy, hard filters and ordering.
        query = urlencode(dict(decision="results", require_verdict="true",
                               sort="triage_score_desc", limit=100, offset=offset))
        payload = self._json(f"/api/real-jobs?{query}")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Invalid Jobfeed canonical Results response")
        return payload

    def _json(self, path):
        base = (self.api_url or os.environ.get("JOBFEED_API_URL", DEFAULT_API_URL)).rstrip("/")
        if self.transport:
            return self.transport(base + path)
        with urlopen(base + path, timeout=30) as response:
            return json.load(response)

    def selected_snapshots(self, real_job_ids):
        """Resolve an exact Results selection, including jobs outside the first page."""
        import hashlib
        if (not isinstance(real_job_ids, (list, tuple)) or not 1 <= len(real_job_ids) <= 100
                or any(not isinstance(i, str) or not i or len(i) > 256 for i in real_job_ids)
                or len(set(real_job_ids)) != len(real_job_ids)):
            raise ValueError("select 1 to 100 distinct canonical job IDs")
        selected = self._json("/api/real-jobs/selection?decision=results&require_verdict=true")
        members = selected.get("real_job_ids") if isinstance(selected, dict) else None
        if not isinstance(members, list):
            raise ValueError("invalid canonical Results selection")
        allowed = set(map(str, members))
        result = []
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for real_id in real_job_ids:
                if real_id not in allowed:
                    raise ValueError("selected job is outside current Results: " + real_id)
                detail = self._json("/api/real-jobs/" + quote(real_id, safe=""))
                if not isinstance(detail, dict):
                    raise ValueError("invalid Jobfeed selected detail")
                job, status = detail.get("job"), detail.get("status")
                evaluation = detail.get("evaluation")
                if not all(isinstance(v, dict) for v in (job, status, evaluation)) or not isinstance(evaluation.get('stage_b'), dict):
                    raise ValueError("incomplete Jobfeed selected detail")
                if (str(detail.get("real_job_id")) != real_id
                        or detail.get("evaluation_stale_reason") or job.get("closed_at")
                        or status.get("decision") != "results"
                        or (evaluation.get("stage_b") or {}).get("verdict") != "apply"):
                    raise ValueError("selected job is no longer eligible: " + real_id)
                source_id = job.get("id")
                if type(source_id) not in (str, int) or not str(source_id).isdigit() or int(source_id) < 1:
                    raise ValueError("invalid selected source job ID")
                source_id = int(source_id)
                mapping = db.execute("SELECT real_job_id FROM jobs WHERE id=?", (source_id,)).fetchone()
                if not mapping or str(mapping[0]) != real_id:
                    raise ValueError("Jobfeed service and local database identity mismatch")
                local = self.get_job(source_id)
                snapshot = dict(real_job_id=real_id, source_job_id=source_id,
                                company=local.company, title=local.title, url=local.url, jd_text=local.description)
                for name in ("company", "title", "url", "jd_text"):
                    if job.get(name) != snapshot[name]:
                        raise ValueError("Jobfeed source content mismatch: " + name)
                inspection_url(snapshot["url"])
                if excluded_company(snapshot['company']):
                    raise ValueError("selected company is excluded by current policy")
                if self.is_applied(source_id):
                    raise ValueError("selected job is already applied: " + real_id)
                snapshot['content_sha256'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                result.append(snapshot)
        return result

    def manual_source_ids(self) -> list[int]:
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(application_queue)")}
            if "manual_priority" not in columns:
                return []
            return [int(row[0]) for row in db.execute(
                "SELECT job_id FROM application_queue WHERE manual_priority=1 ORDER BY rowid"
            )]

    def manual_snapshots(self, source_job_ids: list[int]) -> list[dict]:
        if (not isinstance(source_job_ids, list) or not 1 <= len(source_job_ids) <= 100
                or any(type(i) is not int or i < 1 for i in source_job_ids)
                or len(set(source_job_ids)) != len(source_job_ids)):
            raise ValueError("select 1 to 100 distinct positive source job IDs")
        selected = set(self.manual_source_ids())
        result = []
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for source_id in source_job_ids:
                if source_id not in selected:
                    raise ValueError("source job was not manually selected: " + str(source_id))
                queue = db.execute("SELECT state FROM application_queue WHERE job_id=?", (source_id,)).fetchone()
                if queue is None or queue[0] != "Queued":
                    raise ValueError("manual source job is no longer queued: " + str(source_id))
                job = self.get_job(source_id, require_open=True)
                real_id = db.execute("SELECT real_job_id FROM jobs WHERE id=?", (source_id,)).fetchone()[0]
                if real_id is None:
                    raise ValueError("manual source job has no canonical identity")
                if "real_job_status" in tables:
                    status = db.execute("SELECT status FROM real_job_status WHERE real_job_id=?", (real_id,)).fetchone()
                    if status and status[0] == "ignored":
                        raise ValueError("manual source job was explicitly ignored")
                if excluded_company(job.company):
                    raise ValueError("selected company is excluded by current policy")
                if self.is_applied(source_id):
                    raise ValueError("selected source job is already applied: " + str(source_id))
                inspection_url(job.url)
                result.append(dict(real_job_id=str(real_id), source_job_id=source_id,
                                   company=job.company, title=job.title, url=job.url, jd_text=job.description))
        return result

    def priority_jobs(self, *, limit: int = 100, include_queued: bool = False,
                      verdicts: frozenset[str] = frozenset({"apply"}), skip=None) -> list[Job]:
        """Consume current eligible Results whose Stage B verdict is in `verdicts`, in service order, never legacy scores.

        A job for which `skip(job)` is true is left out and does not count toward `limit`.
        """
        if isinstance(limit, bool) or limit < 0 or limit > 100:
            raise ValueError("priority list limit must be between 0 and 100")
        if limit == 0:
            return []
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            queued = connection.execute(
                "SELECT q.job_id,j.real_job_id FROM application_queue q "
                "JOIN jobs j ON j.id=q.job_id"
                + (" WHERE q.state<>'Queued'" if include_queued else "")
            ).fetchall()
            queued_ids = {int(row[0]) for row in queued}
            seen = set()
            result = []
            offset = 0
            manual_ids = set(self.manual_source_ids())
            while len(result) < limit or manual_ids - seen - queued_ids:
                payload = self._canonical_page(offset)
                rows = payload["jobs"]
                if not rows:
                    break
                for row in rows:
                    real_id = str(row["real_job_id"])
                    if (row.get("verdict") not in verdicts or row.get("decision") != "results"
                            or row.get("evaluation_stale_reason") or row.get("closed_at")):
                        continue
                    source_id = int(row["source_job_id"])
                    if source_id in seen or source_id in queued_ids or self.is_applied(source_id):
                        continue
                    seen.add(source_id)
                    mapping = connection.execute("SELECT real_job_id FROM jobs WHERE id=?", (source_id,)).fetchone()
                    if not mapping or str(mapping[0]) != real_id:
                        raise ValueError("Jobfeed service and local database identity mismatch")
                    job = self.dispatchable_job(source_id)
                    if job is None or (skip is not None and skip(job)):
                        continue
                    result.append(job)
                    if len(result) >= limit and not manual_ids - seen - queued_ids:
                        break
                offset += len(rows)
                if offset >= int(payload["total"]):
                    break
            return sorted(result, key=lambda job: job.id not in manual_ids)[:limit]

    def recruiter_sheet_holds(self) -> frozenset[int]:
        """Source jobs on the recruiter sheet that autopilot must not apply to. Raises if the sheet cannot be read."""
        from jobfeed.config import load_settings
        from jobfeed.domain.recruiter_sheet import held_source_ids
        from jobfeed.recruiter_sheet import read_sheet
        policy = load_settings(self.repo_root / "config.toml").recruiter_sheet.to_domain()
        return held_source_ids(read_sheet(self.db_path, policy))

    def dispatchable_job(self, job_id: int) -> Job | None:
        try:
            return self.get_job(job_id, require_open=True)
        except (KeyError, ValueError):
            return None

    def get_job(self, job_id: int, *, require_open: bool = False) -> Job:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(f"Jobfeed job {job_id} does not exist")
        if require_open:
            if "closed_at" in row.keys() and row["closed_at"]:
                raise ValueError(f"Jobfeed job {job_id} is closed")
            if any(not row[field] or not str(row[field]).strip()
                   for field in ("company", "title", "jd_text", "url")):
                raise ValueError(f"Jobfeed job {job_id} has incomplete source details")
        if not row["jd_text"]:
            raise ValueError(f"Jobfeed job {job_id} has no complete JD")
        if not row["url"]:
            raise ValueError(f"Jobfeed job {job_id} has no application URL")
        return Job(
            id=int(row["id"]),
            company=str(row["company"]),
            title=str(row["title"]),
            description=str(row["jd_text"]),
            url=str(row["url"]),
        )

    @staticmethod
    def _applied_predicate(connection: sqlite3.Connection) -> str:
        """Shared receipt check for the source posting aliased as j."""
        legacy = "j.id IN (SELECT job_id FROM applied WHERE job_id IS NOT NULL)"
        canonical = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='real_job_applications'"
        ).fetchone()
        if canonical is None:
            return legacy
        return legacy + """ OR j.id IN (
            SELECT source_job_id FROM real_job_applications WHERE source_job_id IS NOT NULL
        ) OR j.id IN (
            SELECT source_applied_job_id FROM real_job_applications WHERE source_applied_job_id IS NOT NULL
        )"""

    def is_applied(self, job_id: int) -> bool:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            predicate = self._applied_predicate(connection)
            row = connection.execute(
                f"SELECT 1 FROM jobs AS j WHERE j.id=? AND ({predicate})", (job_id,)
            ).fetchone()
            return row is not None

    def record_applied(self, job_id: int, application_folder: Path) -> str:
        if self.is_applied(job_id):
            return "Already recorded in Jobfeed (read back confirmed)"
        from jobfeed.submission_evidence import read_confirmation
        job = self.get_job(job_id)
        read_confirmation(application_folder, job_id, job.url, company=job.company)
        pointer_path = application_folder / 'resume-source.json'
        pointer = json.loads(pointer_path.read_text()) if pointer_path.exists() else {}
        selected = None
        if pointer.get('kind') == 'selected_existing':
            from pipeline.resume_catalog import selected_materials
            selected = selected_materials(application_folder, job_id=job_id)
        tailored = Path(selected['paths']['text']) if selected and 'text' in selected['paths'] else None
        if not selected:
            tailored = application_folder / 'tailored.md'
            if not tailored.is_file():
                raise FileNotFoundError(f"missing tailored resume source: {tailored}")
        executable = self.repo_root / ".venv" / "bin" / "jobfeed"
        notes = 'Recorded by resume application pipeline after confirmed submission'
        if selected:
            notes += '; uploaded existing resume ID=' + selected['resume_id'] + '; uploaded PDF SHA256=' + selected['assets']['pdf']['sha256'] + '; configured Jobfeed master hash is separate'
        command = [str(executable if executable.exists() else "jobfeed"), "--config", str(self.repo_root / "config.toml"),
                   "apply", str(job_id), "--method", "web", "--notes", notes,
                   "--confirmation-folder", str(application_folder.resolve())]
        if tailored:
            command.extend(['--tailored', str(tailored)])
        if selected:
            command.extend(['--variant', selected['resume_id']])
        completed = subprocess.run(
            command,
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        if not self.is_applied(job_id):
            raise RuntimeError(f"Jobfeed apply service did not persist job {job_id}")
        return completed.stdout.strip()

    def is_ignored(self, job_id: int) -> bool:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                "SELECT s.status FROM jobs j JOIN real_job_status s ON s.real_job_id=j.real_job_id WHERE j.id=?", (job_id,)
            ).fetchone()
            return row is not None and row[0] == "ignored"

    def record_ignored(self, job_id: int, evidence: str, prefix: str = "Employer posting closed: ") -> str:
        if self.is_applied(job_id):
            raise ValueError("Cannot ignore an applied job")
        executable = self.repo_root / ".venv" / "bin" / "jobfeed"
        completed = subprocess.run([
            str(executable if executable.exists() else "jobfeed"), "--config", str(self.repo_root / "config.toml"),
            "mark", str(job_id), "--status", "ignored", "--note", prefix + evidence,
        ], cwd=self.repo_root, text=True, capture_output=True, timeout=120, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        if not self.is_ignored(job_id):
            raise RuntimeError(f"Jobfeed ignored service did not persist job {job_id}")
        return completed.stdout.strip()
