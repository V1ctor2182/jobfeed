import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pipeline.batch import BatchPipeline
class ValidationBatchTest(unittest.TestCase):
 def test_claude_is_the_only_runtime(self):
  with tempfile.TemporaryDirectory() as d:
   f=Path(d)/'batch.json'
   with patch('pipeline.orchestrator.ApplicationPipeline.__init__',lambda s,*a,**kw:setattr(s,'applications',SimpleNamespace())):
    for runtime in ({},{'application_runtime':'claude'}):
     f.write_text(json.dumps({'ordered_candidates':[{'id':1}],'max_parallel':1,'target_successful_submissions':1,**runtime}))
     self.assertEqual(BatchPipeline(None,f).batch_ids,(1,))
    f.write_text(json.dumps({'ordered_candidates':[{'id':1}],'max_parallel':1,'target_successful_submissions':1,'application_runtime':'codex'}))
    with self.assertRaisesRegex(ValueError,'Codex runtime was removed'):BatchPipeline(None,f)
