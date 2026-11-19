"""A synthetic application runtime: a resume root with one catalog PDF, and a Jobfeed database with 102 jobs."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class RuntimeFixture:
    def __init__(self, base):
        self.root = (Path(base) / 'resume').resolve()
        self.root.mkdir(exist_ok=True)
        (self.root / '.venv/bin').mkdir(parents=True, exist_ok=True)
        (self.root / '.venv/bin/python').write_text('fixture boundary never execute')
        self.db = Path(base) / 'jobfeed/data/jobfeed.sqlite'
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,real_job_id TEXT,company TEXT,title TEXT,jd_text TEXT,url TEXT); CREATE TABLE applied(job_id INTEGER);')
            db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?)', [(i, str(1000+i), 'Fixture Company', 'Engineer '+str(i), 'Complete JD '+str(i), 'https://fixture.invalid/jobs/'+str(i)) for i in range(1, 103)])
        (self.root / 'fixture.pdf').write_bytes(b'%PDF-1.4 synthetic existing resume')
        (self.root / 'resume-catalog.json').write_text(json.dumps({'schema_version': 1, 'resumes': [{'id': 'fixture-original', 'label': 'Fixture original', 'available': True, 'pdf': 'fixture.pdf'}]}))
