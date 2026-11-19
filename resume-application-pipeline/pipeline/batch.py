"""Bounded execution of an explicitly selected, already populated batch, or of the continuous autopilot batch."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from pipeline.orchestrator import ApplicationPipeline
from pipeline.lifecycle import ApplicationState as S, TERMINAL_STATES

class BatchPipeline(ApplicationPipeline):
    def __init__(self, config, manifest, **kwargs):
        super().__init__(config, **kwargs)
        self.manifest_path = Path(manifest).resolve()
        self.applications.execution_scope = str(self.manifest_path)
        self._apply_manifest(json.loads(self.manifest_path.read_text()))
        # A reviewed batch can correct source-company aliases and migrated URLs.
        if hasattr(self, "jobfeed"):
            source_get_job = self.jobfeed.get_job
            def get_batch_job(job_id, *, require_open=False):
                job = source_get_job(job_id, require_open=require_open)
                snapshot = self.candidate_snapshots.get(job_id, {})
                return replace(job, company=snapshot.get("company", job.company),
                               title=snapshot.get("title", job.title),
                               url=snapshot.get("url", job.url),
                               description=snapshot.get("jd_text") or job.description)
            self.jobfeed.get_job = get_batch_job

    def reload_manifest(self):
        """Apply an edited manifest in place so dispatch need not wait for a restart."""
        self._apply_manifest(json.loads(self.manifest_path.read_text()))

    def _apply_manifest(self, data):
        config = getattr(self, "config", None)
        if data.get('bridge_managed'):
            raise ValueError('bridge-managed manifests came from the removed Codex batch bridge')
        if data.get("application_runtime", "claude") != "claude":
            raise ValueError("application_runtime must be claude: the Codex runtime was removed")
        from pipeline.progress_monitor import digest
        self.loaded_manifest_hash = digest(data)
        self.deadline = datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None
        self.target_ids = tuple(int(j["id"]) for j in data["ordered_candidates"])
        candidates = data["ordered_candidates"] + data.get("carryover_candidates", [])
        self.candidate_snapshots = {int(j["id"]): j for j in candidates}
        self.batch_ids = tuple(self.candidate_snapshots)
        self.suspended_ids = {int(i) for i in data.get("suspended_job_ids", [])}
        self.batch_limit = int(data['max_parallel'])
        # The autopilot batch admits eligible jobs itself while the user's autopilot mode is on.
        self.continuous = data.get('continuous') is True
        self.batch_target = int(data['target_successful_submissions'])
        # Appliers link their tab to this Dashboard's Open application form button.
        self.dashboard_url = data.get("dashboard_url")
        self.resume_catalog = data.get("resume_catalog")
        if config is not None:
            from pipeline.resume_catalog import verify_catalog
            verify_catalog(config.resume_root, self.resume_catalog)

    def batch_items(self):
        by_id = {i.job_id:i for i in self.queue.list_active()}
        items = [by_id[i] for i in self.batch_ids if i in by_id
                 and i not in getattr(self, "suspended_ids", set())
                 and by_id[i].state not in TERMINAL_STATES
                 and not (i in getattr(self, "target_ids", ())
                          and by_id[i].state == S.NEEDS_INPUT)]
        if not getattr(self, 'continuous', False):
            return items
        from pipeline.autopilot import startable
        return startable(self, items)

    def successes(self):
        return sum(self.confirmation_available(i) for i in getattr(self, "target_ids", self.batch_ids))

    def uncertain_submissions(self):
        pending = set()
        for job_id in getattr(self, 'target_ids', self.batch_ids):
            item = self.queue.get(job_id)
            reported = bool(self._user_submission(job_id)) or bool(item and item.state == S.SUBMISSION_UNKNOWN)
            if reported and not self.confirmation_available(job_id):
                pending.add(job_id)
        return pending

    def refill_queue(self, active_capacity):
        if not getattr(self, 'continuous', False):
            return []  # The user selected the source snapshots before starting this batch.
        from pipeline.autopilot import refill
        return refill(self)

    def autopilot_off(self):
        from pipeline.autopilot import enabled
        return getattr(self, 'continuous', False) and not enabled(self.config.resume_root)

    def finished(self, snapshot):
        """Whether the worker may stop. The continuous batch never stops while autopilot is on."""
        if not getattr(self, 'continuous', False):
            return snapshot['complete']
        from pipeline.autopilot import finished
        return self.autopilot_off() and finished(snapshot)

    def deadline_reached(self):
        deadline = getattr(self, "deadline", None)
        return deadline is not None and datetime.now(timezone.utc) >= deadline

    def can_start_application(self, additional_reservations=0):
        if self.deadline_reached():
            return False
        uncertain = self.uncertain_submissions()
        reserved = len(uncertain) + sum(i.state not in {S.QUEUED,S.DISPATCHING,S.TAILORING,S.TAILORING_PAUSED,S.RESUME_READY,S.DEFERRED}
                       for i in self.batch_items() if i.job_id not in uncertain)
        return self.successes() + reserved + additional_reservations < self.batch_target

    def can_start_application_for(self, job_id, additional_reservations=0):
        if self.autopilot_off():
            return False
        gate = getattr(self, 'browser_gate', None)
        if gate is not None and not gate.ready():
            return False
        return self.can_start_application(additional_reservations)

    def start_ready_application(self, job_id):
        self._assert_not_paused(job_id)
        if job_id not in self.batch_ids or not self.can_start_application_for(job_id):
            return {'status':'batch_limit'}
        self._require(job_id,S.RESUME_READY)
        return super().start_ready_application(job_id)
