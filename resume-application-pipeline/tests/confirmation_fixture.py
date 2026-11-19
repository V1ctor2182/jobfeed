"""Synthetic confirmation artifacts for isolated tests only."""
import json
from PIL import Image
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path


def write_confirmation(folder: Path, job_id=1, job_url='https://example.com/jobs/1', *, kind='website_screenshot', text='Your application has been received.'):
    folder.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(UTC).isoformat()
    capture = {'tool': 'synthetic_test_fixture', 'source_id': 'test-tab-1'}
    confirmation = {'kind': kind, 'captured_at': captured_at, 'source_url': job_url + '/confirmation', 'capture': capture}
    if kind == 'website_screenshot':
        Image.new('RGB', (100, 50), 'white').save(folder / 'confirmation.png')
        confirmation.update(artifact='confirmation.png', observation='confirmation-page.json', text=text)
        (folder / 'confirmation-page.json').write_text(json.dumps({'job_id': job_id, 'job_url': job_url, 'source_url': confirmation['source_url'], 'captured_at': captured_at, 'capture': capture, 'text': text}))
    else:
        message = EmailMessage()
        message['From'] = 'careers@example.com'
        message['To'] = 'applicant@example.net'
        message['Subject'] = 'Application confirmation'
        message['Message-ID'] = '<synthetic-1@example.com>'
        message['Date'] = format_datetime(datetime.now(UTC) - timedelta(seconds=10))
        message.set_content(text + '\n' + job_url)
        (folder / 'confirmation.eml').write_bytes(message.as_bytes())
        capture['source_id'] = message['Message-ID']
        confirmation.update(artifact='confirmation.eml', source_url='https://mail.example.com/message/1')
    receipt = {'job_id': job_id, 'job_url': job_url, 'confirmation': confirmation}
    (folder / 'submission-receipt.json').write_text(json.dumps(receipt))
    return receipt
