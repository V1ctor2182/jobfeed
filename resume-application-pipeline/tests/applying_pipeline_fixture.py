"""A plain ApplicationPipeline whose one job is applying in its own folder under a Claude Applier owner; synthetic data only."""
from contextlib import closing
from pathlib import Path
import sqlite3

from pipeline.config import PipelineConfig
from pipeline.lifecycle import ApplicationState as S
from pipeline.orchestrator import ApplicationPipeline

OWNER = 'c' * 32


def applying_pipeline(root, job_id=1, folder='one', *, jobfeed=None):
    root = Path(root)
    config = PipelineConfig(root, root, root / 'jobs.sqlite', root / 'applications')
    with closing(sqlite3.connect(config.jobfeed_db)) as db, db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO jobs VALUES(?)', (job_id,))
    pipeline = ApplicationPipeline(config, jobfeed=jobfeed)
    pipeline.initialize()
    pipeline.queue.enqueue(job_id)
    pipeline.queue.transition(job_id, S.RESUME_READY, application_folder_relpath=folder)
    pipeline.queue.transition(job_id, S.APPLYING, codex_application_session_id=OWNER)
    (config.applications_root / folder).mkdir(parents=True)
    return pipeline
