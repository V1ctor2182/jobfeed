"""Verify CUA profile inventory against the running Jobfeed Chrome extension."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pipeline.application_rerun import RerunError


def tab_candidate(value):
    if not isinstance(value, dict) or set(value) != {'browser_id', 'native_tab_id', 'url'}:
        raise RerunError('A browser ID, native tab ID and exact URL are required.', 'invalid')
    if (not isinstance(value['browser_id'], str) or not value['browser_id']
            or type(value['native_tab_id']) is not int or value['native_tab_id'] <= 0
            or not isinstance(value['url'], str) or not 0 < len(value['url']) <= 4096):
        raise RerunError('Invalid browser tab candidate.', 'invalid')
    return value


def candidates(payload):
    if not isinstance(payload, dict) or set(payload) != {'candidates'}:
        raise RerunError('Supply fresh CUA candidates on stdin.', 'invalid')
    values = payload['candidates']
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise RerunError('Provide between 1 and 16 existing Chrome tab candidates.', 'invalid')
    normalized = []
    for value in values:
        required = {'browser_id', 'native_tab_id', 'url', 'extension_instance_id'}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'profile_name'}:
            raise RerunError('Each candidate needs its fresh CUA browser, extension instance and profile metadata.', 'invalid')
        tab_candidate({key: value[key] for key in ('browser_id', 'native_tab_id', 'url')})
        if (not isinstance(value['extension_instance_id'], str) or not 0 < len(value['extension_instance_id']) <= 256
                or not isinstance(value.get('profile_name', ''), str) or len(value.get('profile_name', '')) > 256):
            raise RerunError('Missing CUA Chrome profile identity.', 'invalid')
        normalized.append({'profile_name': '', **value})
    return normalized


class ProfileBridge:
    def __init__(self, base_url):
        parsed = urlsplit(base_url or '')
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise RerunError('Use the running local dashboard to restart and verify Chrome.', 'unavailable')
        self.base_url = base_url

    def request(self, path, payload=None):
        headers = {'Accept': 'application/json'}
        data = None
        if payload is not None:
            state = self.request('/api/application-tabs')
            headers.update({'Content-Type': 'application/json', 'Origin': self.base_url,
                            'X-Application-Tabs-CSRF': state['csrf_token']})
            data = json.dumps(payload).encode()
        try:
            with urlopen(Request(self.base_url + path, data=data, headers=headers), timeout=20) as response:
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError('oversized response')
                return json.loads(body)
        except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
            detail = ''
            if isinstance(exc, HTTPError):
                try:
                    error = json.loads(exc.read(8192))
                    detail = error.get('error', {}).get('message', '')
                except (ValueError, AttributeError, TypeError):
                    pass
            raise RerunError(detail or 'The Jobfeed Chrome profile could not be verified. Keep the existing form open.', 'unavailable') from None

    def available(self):
        state = self.request('/api/application-tabs')
        if state.get('profile_probe_available') is not True:
            raise RerunError(state.get('profile_probe_reason') or 'Reload the Jobfeed extension in the intended Chrome profile before restarting.', 'unavailable')

    def probe(self, inventory):
        return self.request('/api/application-tabs/profile-probe', {'candidates': [
            {'native_tab_id': item['native_tab_id'], 'url': item['url']} for item in inventory]})

    def validate(self, proof):
        if not proof:
            raise RerunError('Verify the Jobfeed Chrome profile before closing any application tab.')
        return self.request('/api/application-tabs/profile-validate', {'profile_proof': proof})

    def connect(self, job_id, owner, proof, tab):
        return self.request(f'/api/application-tabs/{job_id}/auto-connect', {
            'owner_thread_id': owner, 'profile_proof': proof,
            'native_tab_id': tab['native_tab_id'], 'url': tab['url']})


def connect_recorded_tab(config, scope, job_id, owner, payload, dashboard_url=None):
    from pipeline.coordination import CoordinationStore, _origin
    if not isinstance(payload, dict) or set(payload) != {'candidates', 'target'}:
        raise RerunError('Supply fresh CUA candidates and the exact recorded target.', 'invalid')
    inventory = candidates({'candidates': payload['candidates']})
    target = tab_candidate(payload['target'])
    snapshot = CoordinationStore(config.resume_root, scope=scope, jobfeed_db=config.jobfeed_db).snapshot([job_id])
    if not any(row['owner'] == owner for row in snapshot['bindings']):
        raise RerunError('The original owner binding is required.')
    if not any(row['owner'] == owner and row['browser_id'] == target['browser_id']
               and str(row['tab_id']) == str(target['native_tab_id']) and row['origin'] == _origin(target['url'])
               and row['state'] in {'observed', 'preserved'} for row in snapshot['tabs']):
        raise RerunError('Record this exact form tab through resources observe before connecting it.')
    if not dashboard_url:
        raise RerunError('The Dashboard URL is required to connect the recorded form.')
    bridge = ProfileBridge(dashboard_url)
    proof = bridge.probe(inventory)
    matches = {(item['native_tab_id'], item['url']) for item in proof['matches']}
    found = [item for item in inventory if (item['native_tab_id'], item['url']) in matches]
    profiles = {(item['browser_id'], item['extension_instance_id']) for item in found}
    exact = [item for item in found if all(item[key] == target[key] for key in target)]
    if len(profiles) != 1 or len(exact) != 1:
        raise RerunError('The exact recorded form does not uniquely match the connected Chrome profile. Keep it open.')
    binding = bridge.connect(job_id, owner, proof['profile_proof'], target)
    if binding.get('job_id') != job_id or binding.get('owner_thread_id') != owner or binding.get('state') != 'connected':
        raise RerunError('The form jump is not connected. Keep the original tab open and report the connection failure.')
    return binding
