import unittest
from pathlib import Path
from tools.build_library import variants
from tools.content_autofill import convert_content
class ContentAutofillTests(unittest.TestCase):
 def test_both_variants_and_role_boundaries(self):
  src=Path('bases/general/content.py').read_text()
  for tag,text in variants(src):
   data=convert_content(text,tag)
   self.assertEqual(data['education'][0]['grad_year'],'2027' if tag=='May2027' else '2026')
   self.assertEqual(data['education'][0]['major']['name'],'Computer Engineering')
   self.assertEqual([len(x['description_stripped'].split('\n')) for x in data['experience']],[3,2,3])
   self.assertNotIn('Jobfeed',data['experience'][-1]['description_stripped'])
   self.assertEqual(len(data['websites']),2)
   self.assertEqual(data['experience'][1]['company']['name'],'Example Software LLC')
 def test_email_projects_from_actual_content(self):
  data=convert_content(Path('bases/general/content.py').read_text(),'Dec2026')
  self.assertEqual(data['email'],'candidate@example.com')
  self.assertEqual(data['projects'][0]['name'],'Example Project — Demonstration Application')
  self.assertEqual(len(data['projects'][0]['description_stripped'].split('\n')),4)
