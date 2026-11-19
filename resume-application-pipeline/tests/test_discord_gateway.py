import json
import threading
import unittest

from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from pipeline.discord_gateway import Gateway


class FakeSocket:
    def __init__(self, events, close_code=None):
        self.events, self.sent, self.close_code = events, [], close_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        return json.dumps({'op': 10, 'd': {'heartbeat_interval': 60000}})

    def __iter__(self):
        for event in self.events:
            yield json.dumps(event)
        if self.close_code is not None:
            raise ConnectionClosed(Close(self.close_code, 'closed'), None)

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self, *args):
        pass


class GatewayTests(unittest.TestCase):
    def test_identifies_without_intents_and_hands_each_interaction_to_the_bridge(self):
        pressed, logs, done = [], [], threading.Event()
        press = {'id': '1', 'type': 3, 'data': {'custom_id': 'answer:x'}}
        socket = FakeSocket([{'op': 11}, {'op': 0, 's': 1, 't': 'READY', 'd': {}}, {'op': 0, 's': 2, 't': 'INTERACTION_CREATE', 'd': press}, {'op': 7}])

        def on_interaction(interaction):
            pressed.append(interaction)
            done.set()
            return {'event': 'interaction'}
        gateway = Gateway('token', on_interaction, connect=lambda *a, **k: socket, log=logs.append)
        gateway.session()
        self.assertTrue(done.wait(2))
        self.assertEqual(pressed, [press])
        identify = socket.sent[0]
        self.assertEqual((identify['op'], identify['d']['token'], identify['d']['intents']), (2, 'token', 0))

    def test_a_rejected_token_stops_instead_of_reconnecting(self):
        connects, logs = [], []

        def connect(*args, **kwargs):
            connects.append(1)
            return FakeSocket([], close_code=4004)
        Gateway('bad', lambda i: None, connect=connect, log=logs.append).run()
        self.assertEqual(len(connects), 1)
        self.assertEqual(logs[-1]['event'], 'gateway_stopped')

    def test_a_dropped_connection_reconnects(self):
        connects, logs = [], []
        gateway = Gateway('token', lambda i: None, log=logs.append, max_delay=0)

        def connect(*args, **kwargs):
            connects.append(1)
            if len(connects) == 3:
                gateway.stop.set()
            return FakeSocket([], close_code=1006)
        gateway.connect = connect
        gateway.run()
        self.assertEqual(len(connects), 3)

    def test_a_failing_handler_is_logged_and_the_listener_keeps_going(self):
        logs, done = [], threading.Event()
        socket = FakeSocket([{'op': 0, 's': 1, 't': 'INTERACTION_CREATE', 'd': {'id': '1'}}, {'op': 7}])

        def log(event):
            logs.append(event)
            if event['event'] == 'interaction_failed':
                done.set()
        Gateway('token', lambda i: 1 / 0, connect=lambda *a, **k: socket, log=log).session()
        self.assertTrue(done.wait(2))


if __name__ == '__main__':
    unittest.main()
