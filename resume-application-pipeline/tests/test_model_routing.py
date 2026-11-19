import unittest

from pipeline.model_routing import is_strong_ats

STRONG = ('https://visa.wd5.myworkdayjobs.com/en-US/Visa/job/1',
          'https://uscareers-cvent.icims.com/jobs/8265/software-engineer-i/job',
          'https://jobs.lever.co/acme/5', 'https://taxfyle.applytojob.com/apply/1', 'https://lenovo.avature.net/careers/1')
DEFAULT = ('https://jobs.ashbyhq.com/etched/2', 'https://boards.greenhouse.io/co/jobs/1', 'https://www.linkedin.com/jobs/3')


class StrongAtsTest(unittest.TestCase):
    def test_complex_ats_hosts_take_the_strong_model_and_simple_ones_the_default(self):
        # 2026-10-15/16: Workday, iCIMS and the ATSs ranked by trial history take the strong model.
        for url in STRONG:
            self.assertTrue(is_strong_ats(url), url)
        for url in DEFAULT:
            self.assertFalse(is_strong_ats(url), url)

    def test_any_strong_url_among_several_counts(self):
        self.assertTrue(is_strong_ats(None, DEFAULT[0], STRONG[0]))
        self.assertFalse(is_strong_ats(None, ''))


if __name__ == '__main__':
    unittest.main()
