import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from pipeline.batch import BatchPipeline
from pipeline.config import PipelineConfig
from pipeline.progress_monitor import digest
from pipeline.resume_catalog import ResumeCatalog
from pipeline.worker import OrchestratorWorker, WorkerSettings


def manifest_data(catalog, suspended=()):
    return dict(ordered_candidates=[dict(id=7, company='Acme', title='Engineer', url='https://jobs.ashbyhq.com/a/7')],
                max_parallel=4, target_successful_submissions=5, resume_mode='selected_existing', resume_catalog=catalog,
                suspended_job_ids=list(suspended))


class ManifestReloadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'batch.json'
        (self.root / 'fixture.pdf').write_bytes(b'%PDF-1.4 synthetic existing resume')
        (self.root / 'resume-catalog.json').write_text(json.dumps({'schema_version': 1, 'resumes': [{'id': 'fixture', 'label': 'Fixture', 'available': True, 'pdf': 'fixture.pdf'}]}))
        self.catalog = ResumeCatalog.load(self.root / 'resume-catalog.json').freeze(self.root)
        self.path.write_text(json.dumps(manifest_data(self.catalog)))
        self.batch = BatchPipeline(PipelineConfig(self.root, self.root, self.root / 'db', self.root / 'apps'), self.path)

    def test_batch_reload_applies_new_manifest(self):
        self.path.write_text(json.dumps(manifest_data(self.catalog, suspended=[7])))
        self.batch.reload_manifest()
        self.assertEqual(self.batch.suspended_ids, {7})
        self.assertEqual(self.batch.loaded_manifest_hash, digest(manifest_data(self.catalog, suspended=[7])))

    def test_worker_reloads_instead_of_stopping_dispatch(self):
        monitor = Mock()
        snapshots = [dict(config_hash=digest(manifest_data(self.catalog)), complete=False),
                     dict(config_hash=digest(manifest_data(self.catalog, suspended=[7])), complete=False)]
        monitor.tick.side_effect = snapshots
        events = []
        w = OrchestratorWorker(self.batch, WorkerSettings(), emit=events.append, progress_monitor=monitor)
        self.addCleanup(w.close)
        w._check_progress()
        self.path.write_text(json.dumps(manifest_data(self.catalog, suspended=[7])))
        w._check_progress()
        self.assertIsNotNone(w._progress_snapshot)
        self.assertEqual(self.batch.suspended_ids, {7})
        self.assertIn('manifest_reloaded', [e['event'] for e in events])
        self.assertNotIn('progress_check_failed', [e['event'] for e in events])


if __name__ == '__main__':
    unittest.main()
