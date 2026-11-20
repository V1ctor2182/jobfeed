"""Whether the applying browser can take a new Applier: its Claude in Chrome extension must be running.

2026-11-18: earlyoom stopped the extension's process while the browser itself kept running. For seven hours every new
Applier found no applying browser, reported browser_disconnected and parked, 124 of them, each holding its session.
Once the extension runs again, the worker answers Done to every such blocker reported before then, so nobody has to.
While it is down, the worker reloads it from the browser's extensions page, which keeps every tab open.
"""
from __future__ import annotations

import json
import queue
from datetime import datetime, timezone
import time
import urllib.request

from pipeline.browser_broker import BROWSER_URL

EXTENSION = 'fcoeoabgfenejglbffodgkkbkcdhcgfn'
CHECK_EVERY = 30.0
REPLY, SOURCE, SENDER = 'done', 'apply_browser', 'apply-browser-check'
# A reload that does not bring the extension back is tried again after this many seconds.
RELOAD_EVERY = 300.0
_HAS_API = "typeof chrome !== 'undefined' && !!chrome.developerPrivate"
_RELOAD = ("new Promise(done => chrome.developerPrivate.reload('" + EXTENSION + "', {failQuietly: true}, "
           "() => done(chrome.runtime.lastError ? chrome.runtime.lastError.message : 'ok')))")


def extension_running(targets):
    """The extension runs while its offscreen document or service worker does. Its options page alone does not count."""
    prefix = f'chrome-extension://{EXTENSION}/'
    return any(t.get('url', '').startswith(prefix) and t.get('type') != 'page' for t in targets)


def _targets(url=BROWSER_URL):
    with urllib.request.urlopen(f'{url}/json/list', timeout=5) as response:
        return json.load(response)


class ExtensionGate:
    def __init__(self, *, targets=_targets, clock=time.monotonic, wall=lambda: datetime.now(timezone.utc), every=CHECK_EVERY):
        self.targets, self.clock, self.wall, self.every = targets, clock, wall, every
        self._ready, self._checked = None, None
        # When the extension was last seen to start running, or to run at the worker's first check.
        self.ready_since = None
        self.events = queue.SimpleQueue()

    def ready(self):
        now = self.clock()
        if self._checked is None or now - self._checked >= self.every:
            self._checked = now
            try:
                ready = extension_running(self.targets())
            except (OSError, ValueError) as exc:
                ready, reason = False, f'{type(exc).__name__}: {exc}'[:200]
            else:
                reason = '' if ready else 'the Claude in Chrome extension is not running in the applying browser'
            if ready != self._ready:
                self.ready_since = self.wall().isoformat() if ready else None
                self.events.put({'event': 'apply_browser_ready'} if ready else {'event': 'apply_browser_down', 'reason': reason,
                                 'note': 'No new Applier starts until it runs again.'})
            self._ready = ready
        return self._ready

    def drain(self):
        found = []
        while not self.events.empty():
            found.append(self.events.get())
        return found


def answer_outages(store, gate):
    """Answer Done to each open browser_disconnected blocker reported before the extension last started running.

    A blocker reported while it already ran has another cause, such as a signed-out extension, and stays for the user.
    So does an Applier that reports again after this Done, so nothing repeats on its own."""
    if not gate.ready() or gate.ready_since is None:
        return []
    events = []
    for request in store.browser_outages(gate.ready_since):
        try:
            store.reply(request['id'], REPLY, source_kind=SOURCE, source_id=f'apply-browser-{request["id"]}', sender_id=SENDER)
        except ValueError:
            continue  # The request closed between the read and the reply.
        events.append({'event': 'browser_outage_answered', 'job_id': request['job_id'], 'request_id': request['id']})
    return events


def reload_extension(cdp, *, tries=20, sleep=time.sleep):
    """Reload the extension from a background chrome://extensions tab, then close that tab. Returns 'ok' or Chrome's error.

    2026-11-18: earlyoom had left the extension TERMINATED. This reload brought it back with all 42 form tabs still open,
    where restarting the browser would have closed them."""
    target = cdp.call('Target.createTarget', {'url': 'chrome://extensions/', 'background': True})['targetId']
    try:
        session = cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        for _ in range(tries):
            if cdp.call('Runtime.evaluate', {'expression': _HAS_API, 'returnByValue': True}, session).get('result', {}).get('value'):
                break
            sleep(0.5)
        else:
            return 'the extensions page did not load'
        result = cdp.call('Runtime.evaluate', {'expression': _RELOAD, 'awaitPromise': True, 'returnByValue': True}, session)
        return result.get('result', {}).get('value') or 'no answer'
    finally:
        cdp.call('Target.closeTarget', {'targetId': target})
