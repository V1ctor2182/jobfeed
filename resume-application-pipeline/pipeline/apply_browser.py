"""Whether the applying browser can take a new Applier: its Claude in Chrome extension must be running.

2026-11-18: earlyoom stopped the extension's process while the browser itself kept running. For seven hours every new
Applier found no applying browser, reported browser_disconnected and parked, 124 of them, each holding its session.
"""
from __future__ import annotations

import json
import queue
import time
import urllib.request

from pipeline.browser_broker import BROWSER_URL

EXTENSION = 'fcoeoabgfenejglbffodgkkbkcdhcgfn'
CHECK_EVERY = 30.0


def extension_running(targets):
    """The extension runs while its offscreen document or service worker does. Its options page alone does not count."""
    prefix = f'chrome-extension://{EXTENSION}/'
    return any(t.get('url', '').startswith(prefix) and t.get('type') != 'page' for t in targets)


def _targets(url=BROWSER_URL):
    with urllib.request.urlopen(f'{url}/json/list', timeout=5) as response:
        return json.load(response)


class ExtensionGate:
    def __init__(self, *, targets=_targets, clock=time.monotonic, every=CHECK_EVERY):
        self.targets, self.clock, self.every = targets, clock, every
        self._ready, self._checked = None, None
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
                self.events.put({'event': 'apply_browser_ready'} if ready else {'event': 'apply_browser_down', 'reason': reason,
                                 'note': 'No new Applier starts until it runs again.'})
            self._ready = ready
        return self._ready

    def drain(self):
        found = []
        while not self.events.empty():
            found.append(self.events.get())
        return found
