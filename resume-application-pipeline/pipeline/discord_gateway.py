"""Discord gateway listener for button presses and forms, which REST polling never sees.

It identifies with no intents, so Discord sends it only interactions. Each interaction runs on its own thread, because
Discord needs an answer within three seconds even while another press waits on a slow Submit confirmation.
"""
from __future__ import annotations

import json
import random
import threading

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

URL = 'wss://gateway.discord.gg/?v=10&encoding=json'
# Close codes after which reconnecting cannot help, such as a rejected token.
FATAL_CLOSE = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
DISPATCH, HEARTBEAT, IDENTIFY, RECONNECT, INVALID_SESSION, HELLO, HEARTBEAT_ACK = 0, 1, 2, 7, 9, 10, 11


def _print(event):
    print(json.dumps(event), flush=True)


class Gateway:
    def __init__(self, token, on_interaction, *, connect=ws_connect, log=_print, max_delay=60):
        self.token, self.on_interaction, self.connect, self.log = token, on_interaction, connect, log
        self.max_delay = max_delay
        self.stop = threading.Event()

    def start(self):
        thread = threading.Thread(target=self.run, name='discord-gateway', daemon=True)
        thread.start()
        return thread

    def run(self):
        delay = 1
        while not self.stop.is_set():
            try:
                self.session()
                delay = 1
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code in FATAL_CLOSE:
                    self.log({'event': 'gateway_stopped', 'code': code, 'reason': exc.rcvd.reason})
                    return
                self.log({'event': 'gateway_closed', 'code': code})
            except (OSError, TimeoutError, ValueError, KeyError) as exc:
                self.log({'event': 'gateway_error', 'error': str(exc)[:200]})
            self.stop.wait(delay)
            delay = min(delay * 2, self.max_delay)

    def session(self):
        """One connection, from Hello until Discord or the network closes it."""
        with self.connect(URL, open_timeout=20, max_size=2 ** 22) as ws:
            hello = json.loads(ws.recv(timeout=20))
            if hello.get('op') != HELLO:
                raise ValueError('Discord did not start with Hello')
            interval = hello['d']['heartbeat_interval'] / 1000
            state = {'seq': None, 'acked': True}
            done = threading.Event()

            def beat():
                # A missing acknowledgement means a dead connection: close it so run() reconnects.
                if done.wait(interval * random.random()):
                    return
                try:
                    while True:
                        if not state['acked']:
                            ws.close(4000, 'heartbeat not acknowledged')
                            return
                        state['acked'] = False
                        ws.send(json.dumps({'op': HEARTBEAT, 'd': state['seq']}))
                        if done.wait(interval):
                            return
                except ConnectionClosed:
                    return

            threading.Thread(target=beat, name='discord-heartbeat', daemon=True).start()
            try:
                ws.send(json.dumps({'op': IDENTIFY, 'd': {'token': self.token, 'intents': 0,
                                                          'properties': {'os': 'linux', 'browser': 'jobfeed-applier', 'device': 'jobfeed-applier'}}}))
                self.log({'event': 'gateway_connected'})
                for raw in ws:
                    event = json.loads(raw)
                    if event.get('s') is not None:
                        state['seq'] = event['s']
                    if event['op'] == HEARTBEAT_ACK:
                        state['acked'] = True
                    elif event['op'] == HEARTBEAT:
                        ws.send(json.dumps({'op': HEARTBEAT, 'd': state['seq']}))
                    elif event['op'] in (RECONNECT, INVALID_SESSION):
                        return
                    elif event['op'] == DISPATCH and event.get('t') == 'INTERACTION_CREATE':
                        threading.Thread(target=self._handle, args=(event['d'],), name='discord-interaction', daemon=True).start()
                    if self.stop.is_set():
                        return
            finally:
                done.set()

    def _handle(self, interaction):
        try:
            result = self.on_interaction(interaction)
        except Exception as exc:  # A failed press must be logged, not kill the listener thread silently.
            self.log({'event': 'interaction_failed', 'error': str(exc)[:200]})
        else:
            if result:
                self.log(result)
