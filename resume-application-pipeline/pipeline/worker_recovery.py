"""Durable bounded recovery counters, scoped to one explicit batch binding."""
import json
from datetime import datetime, timezone
from pathlib import Path
from pipeline.progress_monitor import atomic_write


class RecoveryBudget:
    def __init__(self, path, binding, limit=3):
        self.path, self.binding, self.limit = Path(path), str(binding), limit

    def read(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {'binding': self.binding, 'actions': {}}
        if data.get('binding') != self.binding:
            raise ValueError('recovery budget belongs to another batch')
        return data

    def allowed(self, key):
        return self.read()['actions'].get(key, {}).get('attempts', 0) < self.limit

    def failure(self, key, reason):
        data = self.read(); old = data['actions'].get(key, {})
        data['actions'][key] = {'attempts': old.get('attempts', 0) + 1, 'reason': str(reason),
                                'at': datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2)+'\n')

    def success(self, key):
        data = self.read()
        if key in data['actions']:
            del data['actions'][key]
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2)+'\n')


def stopped(manifest):
    """Malformed existing stop control fails closed; missing control permits explicit launch."""
    if not manifest:
        return False
    manifest = Path(manifest).resolve()
    path = manifest.with_name(manifest.stem+'-handoff.json')
    if not path.exists():
        return False
    try:
        handoff = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    if handoff.get('manifest') != str(manifest):
        return True
    return any(handoff.get(k) == 'stopped_by_user' for k in ('status','controller_status'))
