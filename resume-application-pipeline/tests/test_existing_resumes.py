
from confirmation_fixture import write_confirmation
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline.config import PipelineConfig
from pipeline.lifecycle import ApplicationState as S
from pipeline.orchestrator import ApplicationPipeline
from pipeline.resume_catalog import ResumeCatalog, local_catalog, select_resume, selected_materials, verify_catalog
from tests.runtime_fixture import RuntimeFixture


class ExistingResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = RuntimeFixture(self.temp.name)
        self.config = PipelineConfig.discover(self.f.root)
        self.pdf = b'%PDF-1.4 synthetic existing resume'

    def pipeline(self):
        pipeline = ApplicationPipeline(self.config)
        pipeline.initialize()
        return pipeline

    def prepare(self, jobs=(1,)):
        self.p = self.pipeline()
        for job in jobs:
            self.p.enqueue(job)
        self.assertEqual(self.p.process_completions(), list(jobs))
        return self.p

    def bind(self, job=1, owner='owner-1'):
        self.p.queue.transition(job, S.APPLYING, codex_application_session_id=owner)
        self.p.applications.execution_gate.bind(str(self.f.root), job, owner)

    def choose(self, job=1, owner='owner-1', resume='fixture-original'):
        return select_resume(self.config, str(self.f.root), job, owner, resume, 'Existing general resume matches this role', binding_wait=0)

    def test_selection_replay_source_change_and_selected_copy_tamper(self):
        p = self.prepare(); self.bind(); first = self.choose()
        (self.f.root/'fixture.pdf').write_bytes(b'%PDF-1.4 changed source')
        self.assertEqual(self.choose(), first)
        self.assertEqual(Path(first['paths']['pdf']).read_bytes(), self.pdf)
        Path(first['paths']['pdf']).write_bytes(b'%PDF-1.4 tampered selected')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.choose()
        with self.assertRaises(ValueError):
            selected_materials(p._folder(p.queue.get(1)), job_id=1, scope=str(self.f.root), owner='owner-1')
        with self.assertRaises(ValueError):
            p.jobfeed.record_applied(1, p._folder(p.queue.get(1)))

    def test_foreign_owner_scope_job_unknown_id_and_contradictory_replay(self):
        self.prepare((1,2)); self.bind(); self.bind(2,'owner-2')
        with self.assertRaises(ValueError): self.choose(owner='owner-2')
        with self.assertRaises(ValueError): select_resume(self.config,'foreign-scope',1,'owner-1','fixture-original','reason',binding_wait=0)
        with self.assertRaises(ValueError): self.choose(resume='unlisted-draft')
        chosen = self.choose()
        self.assertEqual(chosen['resume_id'],'fixture-original')
        with self.assertRaises(ValueError): self.choose(resume='different')
        with self.assertRaises(ValueError): selected_materials(self.p._folder(self.p.queue.get(1)),job_id=2)

    def test_delayed_official_acknowledgment_unblocks_same_selection(self):
        self.prepare()
        def late_binding():
            time.sleep(.15)
            self.bind()
        thread = threading.Thread(target=late_binding);thread.start()
        chosen = select_resume(self.config,str(self.f.root),1,'owner-1','fixture-original','reason',binding_wait=2)
        thread.join()
        self.assertEqual(chosen['job_id'],1)
        self.assertEqual(self.p.queue.get(1).codex_application_session_id,'owner-1')

    def test_two_jobs_and_concurrent_freeze_do_not_share_selected_output(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshots=list(pool.map(lambda _: ResumeCatalog.load(self.f.root/'resume-catalog.json').freeze(self.f.root),range(2)))
        self.assertEqual(snapshots[0],snapshots[1])
        self.prepare((1,2));self.bind();self.bind(2,'owner-2')
        with ThreadPoolExecutor(max_workers=2) as pool:
            choices=list(pool.map(lambda args:self.choose(*args),[(1,'owner-1'),(2,'owner-2')]))
        self.assertNotEqual(choices[0]['paths']['pdf'],choices[1]['paths']['pdf'])
        self.assertEqual([Path(c['paths']['pdf']).read_bytes() for c in choices],[self.pdf,self.pdf])

    def test_neutral_facts_and_mutable_job_catalog_cannot_invent_facts(self):
        self.prepare();self.bind();chosen=self.choose();folder=self.p._folder(self.p.queue.get(1))
        pointer=json.loads((folder/'resume-source.json').read_text())
        from pipeline.resume_catalog import digest,encoded
        invented=encoded({'schemaVersion':1,'experience':[{'company':'invented'}]})
        Path(chosen['paths']['facts']).write_bytes(invented)
        pointer['assets']['facts']['sha256']=digest(invented)
        (folder/'resume-source.json').write_text(json.dumps(pointer))
        with self.assertRaisesRegex(ValueError,'invent'):
            selected_materials(folder)
        contract=json.loads((folder/'resume-catalog.json').read_text());contract['catalog']['entries'][0]['label']='forged'
        (folder/'resume-catalog.json').write_text(json.dumps(contract))
        with self.assertRaises(ValueError):self.choose()

    def test_missing_catalog_and_unlisted_drafts_do_not_become_ready(self):
        (self.f.root/'resume-catalog.json').unlink();p=self.pipeline();p.enqueue(1)
        (self.f.root/'draft.pdf').write_bytes(b'%PDF-1.4 annotated draft')
        with self.assertRaises(FileNotFoundError):p.process_completions()
        self.assertEqual(p.queue.get(1).state,S.QUEUED)

    def test_legacy_tailoring_is_parked(self):
        p=self.pipeline();p.enqueue(1);p.queue.transition(1,S.DISPATCHING);p.queue.transition(1,S.TAILORING)
        self.assertEqual(p.process_completions(),[])
        self.assertEqual(p.queue.get(1).state,S.TAILORING)

    def test_symlink_and_frozen_tamper_are_rejected(self):
        snapshot=local_catalog(self.config)
        asset=snapshot['entries'][0]['assets']['pdf'];target=self.f.root/asset['path'];target.write_bytes(b'%PDF-1.4 changed')
        with self.assertRaisesRegex(ValueError,'digest'):verify_catalog(self.f.root,snapshot)
        source=self.f.root/'linked.pdf';source.symlink_to(self.f.root/'fixture.pdf')
        catalog={'schema_version':1,'resumes':[{'id':'linked','label':'Linked','available':True,'pdf':'linked.pdf'}]}
        (self.f.root/'resume-catalog.json').write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError,'regular file'):ResumeCatalog.load(self.f.root/'resume-catalog.json')

    def test_cancelled_owner_and_inactive_scope_cannot_select(self):
        self.prepare();self.bind()
        self.p.applications.execution_gate.cancel_scope(str(self.f.root),'fixture-stop')
        with self.assertRaises(ValueError):self.choose()
        self.assertFalse((self.p._folder(self.p.queue.get(1))/'resume-source.json').exists())

    def test_pdf_only_writeback_requires_real_applied_readback(self):
        self.prepare();self.bind();self.choose()
        write_confirmation(self.p._folder(self.p.queue.get(1)), 1, self.p.jobfeed.get_job(1).url)
        with patch('pipeline.jobfeed.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='fixture accepted',stderr='')):
            with self.assertRaisesRegex(RuntimeError,'did not persist'):
                self.p.jobfeed.record_applied(1,self.p._folder(self.p.queue.get(1)))
        self.assertFalse(self.p.jobfeed.is_applied(1))

    def test_local_catalog_edits_refuse_new_admission_but_keep_existing_selection(self):
        p=self.prepare();self.bind();chosen=self.choose()
        catalog=json.loads((self.f.root/'resume-catalog.json').read_text());catalog['resumes'][0]['label']='new label'
        (self.f.root/'resume-catalog.json').write_text(json.dumps(catalog))
        p.enqueue(2)
        with self.assertRaisesRegex(ValueError,'local resume catalog changed'):p.process_completions()
        self.assertEqual(self.choose(),chosen)
        self.assertEqual(p.queue.get(2).state,S.QUEUED)

    def test_ack_between_final_binding_check_and_wait_marker_converges(self):
        from pipeline import resume_catalog as resumes
        p=self.prepare()
        original=resumes._bound;calls=[]
        def race(*args):
            result=original(*args);calls.append(1)
            if len(calls)==1:self.bind()
            return result
        with patch('pipeline.resume_catalog._bound',side_effect=race):
            chosen=self.choose()
        self.assertEqual(chosen['owner'],'owner-1')
        self.assertEqual(Path(chosen['paths']['pdf']).read_bytes(),self.pdf)

if __name__=='__main__':unittest.main()
