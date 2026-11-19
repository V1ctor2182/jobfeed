import unittest
from tools.check_application_links import classify
class LinkTests(unittest.TestCase):
 def check(self,html,code=200):return classify(html,'Software Engineer',code,'https://example.com/job/1')
 def test_apply_link(self):
  r=self.check('<h1>Software Engineer</h1><a href="/apply/1">Apply now</a>');self.assertEqual(r['result'],'job_page_with_apply_link');self.assertFalse(r['application_form_verified'])
 def test_home_not_success(self):self.assertEqual(self.check('<h1>Careers</h1>')['result'],'needs_browser')
 def test_closed(self):self.assertEqual(self.check('Software Engineer: This job is closed')['result'],'closed_message')
 def test_restricted_not_closed(self):self.assertEqual(self.check('Access denied',403)['result'],'access_restricted')
 def test_missing_not_closed(self):self.assertEqual(self.check('Not found',404)['result'],'url_not_found')
 def test_script_not_evidence(self):self.assertEqual(self.check('<script>Software Engineer Apply now</script>')['result'],'needs_browser')
if __name__=='__main__':unittest.main()
