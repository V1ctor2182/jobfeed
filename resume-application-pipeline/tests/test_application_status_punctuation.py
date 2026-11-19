import unittest

from pipeline.results import parse_application_result


class ApplicationStatusPunctuationTests(unittest.TestCase):
    def test_completed_close_on_status_line_keeps_confirmation(self):
        result = parse_application_result(
            '申请进度：此前已投递；标签关闭：完成\n'
            '说明：已关闭并核对 70992060、70992066 已消失。'
        )
        self.assertEqual(result['status'], 'already_applied')
        self.assertIn('标签关闭：完成', result['reason'])
        self.assertIn('70992060', result['reason'])

    def test_unrecognized_status_suffix_is_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_application_result(
                '申请进度：此前已投递；未提交\n说明：尚未确认。'
            )
