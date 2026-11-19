import copy
import unittest
from pipeline.blocker_ledger import user_request_message

class UserRequestMessageTests(unittest.TestCase):
    def test_approved_request_keeps_decision_but_not_internal_audit(self):
        request={'action':'是否接受GFS一年诉求时限条款？','applicability':'job:501765',
                 'checked_sources':['manifest','source-thread'],
                 'evidence':{'substance':'一年内提起相关诉求，否则失权；Equal Pay Act除外。',
                             'file':'private-record.json','url':'https://example.invalid/terms'},
                 'semantic_request':'internal-dedup-id','why_unavailable':'internal rationale'}
        original=copy.deepcopy(request)
        message=user_request_message(request)
        self.assertIn('Equal Pay Act',message)
        self.assertIn('是否接受',message)
        for word in ['checked_sources','private-record','source-thread','循环领取','internal','applicability']:
            self.assertNotIn(word,message)
        self.assertLess(len(message),180)
        self.assertEqual(request,original)

    def test_fact_question_needs_no_evidence_dump(self):
        self.assertEqual(user_request_message({'action':'请输入执照编号。','evidence':'form metadata'}),'请输入执照编号。')

    def test_invalid_action_fails_internally(self):
        with self.assertRaises(ValueError):user_request_message({'action':{}})
