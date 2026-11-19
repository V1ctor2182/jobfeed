"""Employer closure wording recognised as closed; transient errors never are (Barclays, trial4 2026-10-16)."""
import unittest

from pipeline.applier_runtime import CLOSED


class ClosedPhraseTests(unittest.TestCase):
    def test_employer_closure_wording_counts_as_closed(self):
        for text in ['We are sorry this job post no longer exists.',
                     'This position is no longer available',
                     'This job is no longer accepting applications',
                     'The role has been filled',
                     'The job posting has expired',
                     'This requisition has been closed',
                     'Job not found. The job you requested was not found.',
                     'The position you are looking for could not be found',
                     'Sorry, this job has been removed',
                     '404 Page Not Found',
                     '该职位已不存在']:
            self.assertTrue(CLOSED.search(text), text)

    def test_transient_errors_are_not_closure(self):
        for text in ['Login failed', 'Tab not found', 'Element not found', 'CAPTCHA required', 'network unavailable',
                     'Password: This field is required.', 'Playwright selector deadline exceeded']:
            self.assertFalse(CLOSED.search(text), text)


if __name__ == '__main__':
    unittest.main()
