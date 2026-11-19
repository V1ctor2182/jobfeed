"""A BatchPipeline holding only queue state, for dispatch-slot and target arithmetic."""
from types import SimpleNamespace

from pipeline.batch import BatchPipeline
from pipeline.store import QueueItem


def batch_stub(states, *, limit, target, batch_ids=None, target_ids=None, confirmed=(), **fields):
    """`states` is (job_id, state) pairs; `confirmed` lists jobs whose submission evidence is on disk."""
    p = object.__new__(BatchPipeline)
    rows = [QueueItem(job_id=job_id, state=state) for job_id, state in states]
    by_id = {row.job_id: row for row in rows}
    p.queue = SimpleNamespace(list_active=lambda: rows, get=by_id.get)
    p.batch_ids = tuple(by_id if batch_ids is None else batch_ids)
    p.target_ids = tuple(p.batch_ids if target_ids is None else target_ids)
    p.batch_limit, p.batch_target = limit, target
    p._user_submission = lambda job_id: {}
    p.confirmation_available = lambda job_id: job_id in confirmed
    vars(p).update(fields)
    return p
