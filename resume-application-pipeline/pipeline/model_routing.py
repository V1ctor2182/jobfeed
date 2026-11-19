"""Model choice for a Claude Applier: complex ATSs start on the strong model."""
from __future__ import annotations

# iCIMS added 2026-10-15 (user decision). 2026-10-16: the rest ranked by trial
# history (Lever 5/11 with a stop every time, JazzHR 3/6, Avature 2/5, Paycom
# 3/4 stops, plus the legacy multi-page ATSs); Ashby/Greenhouse/LinkedIn stay default.
STRONG_ATS_HOSTS = ('myworkdayjobs.com', '.workday.com', 'icims.com',
                    'jobs.lever.co', '.applytojob.com', 'avature.', 'paycomonline.net',
                    'taleo.net', 'oraclecloud.com', 'jobvite.com', '.adp.com', 'ultipro.com', 'ukg.net',
                    'successfactors.', 'eightfold.ai', 'dayforcehcm.com', 'brassring.com', 'clearcompany.com')


def is_strong_ats(*urls) -> bool:
    return any(host in str(url or '').lower() for url in urls for host in STRONG_ATS_HOSTS)
