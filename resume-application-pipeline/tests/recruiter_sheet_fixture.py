"""Give a RuntimeFixture's minimal database the tables the recruiter sheet reads, from Jobfeed's own DDL."""
from contextlib import closing
import json
import sqlite3

from jobfeed.adapters.store._sqlite_schema_metadata import schema_ddl_statements
from jobfeed.config import Settings
from jobfeed.domain.recruiter_sheet import SheetEdit
from jobfeed.recruiter_sheet import apply_edits, read_sheet

TABLES = ('real_jobs', 'real_job_status', 'real_job_applications', 'real_job_evaluations', 'recruiter_sheet_rows', 'company_funding')
QUALIFYING = json.dumps({'fit_analysis': {'score': 85, 'strengths': [{'requirement': 'Python'}], 'gaps': []}})


def install(fixture):
    with closing(sqlite3.connect(fixture.db)) as db, db:
        for statement in schema_ddl_statements():
            for table in TABLES:
                if statement.lstrip().startswith(f'CREATE TABLE {table} '):
                    db.execute(statement.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1))
        db.execute("ALTER TABLE jobs ADD COLUMN location TEXT NOT NULL DEFAULT 'Remote, USA'")
        db.execute('ALTER TABLE jobs ADD COLUMN closed_at TEXT')
        db.execute('INSERT INTO real_jobs(id,representative_job_id) SELECT CAST(real_job_id AS INTEGER),id FROM jobs')
    (fixture.db.parent.parent / 'config.toml').write_text('')


def real_job_id(job_id):
    return 1000 + job_id


def qualify(fixture, job_id):
    """Make the source job a well-paid New York job with a strong apply verdict, which puts it on the sheet."""
    with closing(sqlite3.connect(fixture.db)) as db, db:
        db.execute("UPDATE jobs SET location='New York, NY',jd_text='Pay: $150,000 - $200,000 per year.' WHERE id=?", (job_id,))
        db.execute("INSERT INTO real_job_evaluations(real_job_id,source_job_id,input_jd_text,input_facts_json,stage_b_verdict,stage_b_json,updated_at) "
                   "VALUES(?,?,'jd','{}','apply',?,'2026-11-07T00:00:00.000000Z')", (real_job_id(job_id), job_id, QUALIFYING))


def policy():
    return Settings().recruiter_sheet.to_domain()


def decide(fixture, job_id, status):
    [result] = apply_edits(fixture.db, policy(), [SheetEdit(real_job_id(job_id), status)])
    assert result.outcome == 'applied', result


def rows(fixture):
    return read_sheet(fixture.db, policy())
