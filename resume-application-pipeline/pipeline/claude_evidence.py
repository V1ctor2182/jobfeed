"""Submission evidence for Claude Appliers, taken from the session transcript rather than written by the model.

Claude in Chrome returns screenshots to the model and never to disk, and Appliers cannot write files. Claude Code
keeps every tool result, image bytes included, in the session transcript. This module copies the latest screenshot
and page read from that transcript unchanged and writes the receipt that docs/confirmation-evidence.md describes.
"""
from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import re

SCREENSHOT_TOOLS = frozenset({'mcp__claude-in-chrome__computer'})
PAGE_TOOLS = frozenset({'mcp__claude-in-chrome__get_page_text', 'mcp__claude-in-chrome__read_page'})
SUFFIX = {'image/jpeg': '.jpg', 'image/png': '.png'}


def transcript_path(cwd, session_id, home=None):
    """Find the session transcript by its ID, so a change in how Claude Code names project folders cannot hide it."""
    projects = Path(home or Path.home()) / '.claude' / 'projects'
    expected = projects / re.sub(r'[^A-Za-z0-9]', '-', str(cwd)) / f'{session_id}.jsonl'
    if expected.exists():
        return expected
    found = sorted(projects.glob(f'*/{session_id}.jsonl'))
    if len(found) > 1:
        raise ValueError('more than one transcript has this session ID')
    return found[0] if found else expected


def _when(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def latest_capture(transcript, since, tab_id=None):
    """The newest screenshot and page read of `tab_id` in the transcript after `since`, as the tools returned them."""
    calls, shot, page = {}, None, None
    since = _when(since)
    for line in Path(transcript).read_text().splitlines():
        entry = json.loads(line)
        message = entry.get('message')
        parts = message.get('content') if isinstance(message, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'tool_use':
                calls[part.get('id')] = (part.get('name'), part.get('input') or {})
            elif part.get('type') == 'tool_result' and not part.get('is_error') and entry.get('timestamp') and _when(entry['timestamp']) >= since:
                name, arguments = calls.get(part.get('tool_use_id'), (None, {}))
                if tab_id is not None and arguments.get('tabId') != tab_id:
                    continue
                content = part.get('content')
                content = [{'type': 'text', 'text': content}] if isinstance(content, str) else content if isinstance(content, list) else []
                images = [c for c in content if isinstance(c, dict) and c.get('type') == 'image' and (c.get('source') or {}).get('type') == 'base64']
                texts = [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
                if name in SCREENSHOT_TOOLS and images and arguments.get('action') == 'screenshot':
                    shot = {'source': images[-1]['source'], 'tool_use_id': part['tool_use_id'], 'tab_id': arguments.get('tabId'),
                            'captured_at': _when(entry['timestamp']).isoformat()}
                elif name in PAGE_TOOLS and texts:
                    page = {'text': '\n'.join(texts), 'tool': name, 'tool_use_id': part['tool_use_id'], 'tab_id': arguments.get('tabId')}
    return shot, page


def write_receipt(folder, *, job_id, job_url, source_url, acknowledgement, shot, page):
    """Write confirmation image, page observation, and submission receipt. Refuses anything it cannot attribute."""
    if shot is None:
        raise ValueError('no screenshot of the confirmation page since Submit; take one with the computer tool first')
    if page is None:
        raise ValueError('no page read since Submit; read the confirmation page with get_page_text first')
    if not acknowledgement.strip() or ' '.join(acknowledgement.split()) not in ' '.join(page['text'].split()):
        # A read taken while the page was still changing misses a confirmation the screenshot already shows.
        raise ValueError('the acknowledgement must appear exactly in your newest page read, and it does not. If the page shows it now, '
                         'call get_page_text again on the same tab, then record_confirmation again. Keep the tab open and never click Submit again.')
    if shot['tab_id'] is None or page['tab_id'] is None or shot['tab_id'] != page['tab_id']:
        raise ValueError('the screenshot and the page read must both come from the registered form tab')
    media = shot['source'].get('media_type')
    if media not in SUFFIX:
        raise ValueError(f'unsupported screenshot format {media}')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    artifact = f'confirmation{SUFFIX[media]}'
    (folder / artifact).write_bytes(base64.b64decode(shot['source']['data'], validate=True))
    capture = {'tool': 'claude-in-chrome computer screenshot', 'source_id': f"tab {shot['tab_id']} tool_use {shot['tool_use_id']}"}
    observation = {'job_id': job_id, 'job_url': job_url, 'captured_at': shot['captured_at'], 'source_url': source_url,
                   'text': page['text'], 'capture': capture}
    (folder / 'confirmation-page.json').write_text(json.dumps(observation, indent=2, ensure_ascii=False))
    receipt_path = folder / 'submission-receipt.json'
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    receipt.update(job_id=job_id, job_url=job_url, confirmation={
        'kind': 'website_screenshot', 'artifact': artifact, 'observation': 'confirmation-page.json', 'captured_at': shot['captured_at'],
        'source_url': source_url, 'text': page['text'], 'capture': capture})
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False))
    return {'artifact': artifact, 'captured_at': shot['captured_at']}
