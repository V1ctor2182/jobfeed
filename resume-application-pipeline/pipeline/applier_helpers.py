"""Tested widget helpers that Claude Appliers run through javascript_tool. The Fixer adds them; tests gate them."""
from __future__ import annotations

import json
from pathlib import Path
import re

HELPERS = Path(__file__).resolve().parents[1] / 'applier_helpers'
_NAME = re.compile(r'[a-z0-9][a-z0-9-]{1,63}\Z')
REQUIRED = {'name', 'sites', 'widget', 'description', 'args', 'fixture', 'test'}


def registry(root=HELPERS):
    data = json.loads((Path(root) / 'registry.json').read_text())
    helpers = data.get('helpers')
    if not isinstance(helpers, list):
        raise ValueError('registry.json needs a helpers list')
    return helpers


def problems(root=HELPERS):
    """Every reason the registry cannot be trusted. An empty list means it is consistent."""
    root, found, seen = Path(root), [], set()
    for entry in registry(root):
        name = entry.get('name', '?')
        missing = REQUIRED - set(entry)
        if missing:
            found.append(f'{name}: missing {sorted(missing)}')
            continue
        if not _NAME.fullmatch(name) or name in seen:
            found.append(f'{name}: name must be unique lowercase words joined by hyphens')
        seen.add(name)
        if not isinstance(entry['sites'], list) or not entry['sites'] or not all(isinstance(s, str) and s for s in entry['sites']):
            found.append(f'{name}: sites must be a nonempty list of host suffixes or "*"')
        if not (root / f'{name}.js').is_file():
            found.append(f'{name}: {name}.js is missing')
        fixture = (root / entry['fixture']).resolve()
        if not fixture.is_relative_to(root.resolve() / 'fixtures') or not fixture.is_file():
            found.append(f'{name}: fixture must be a file under fixtures/')
        test = entry['test']
        if not isinstance(test, dict) or not isinstance(test.get('args'), dict) or not isinstance(test.get('expect'), dict) or not test['expect']:
            found.append(f'{name}: test needs args and a nonempty expect')
    return found


def for_site(host, root=HELPERS):
    host = (host or '').lower()
    return [{'name': e['name'], 'widget': e['widget'], 'description': e['description'], 'args': e['args']}
            for e in registry(root) if any(s == '*' or host == s or host.endswith('.' + s.lstrip('.')) for s in e['sites'])]


def script(name, args, root=HELPERS):
    """Return code that javascript_tool can run as is."""
    if not _NAME.fullmatch(name or '') or name not in {e['name'] for e in registry(root)}:
        raise ValueError(f'unknown helper {name}')
    if not isinstance(args, dict):
        raise ValueError('helper args must be an object')
    return f'({(Path(root) / f"{name}.js").read_text().strip()})({json.dumps(args)})'
