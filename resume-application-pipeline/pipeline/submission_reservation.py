from contextlib import closing, contextmanager
import fcntl
from pathlib import Path
import sqlite3
import threading

from pipeline.applier_runtime import read_only

_local = threading.local()


@contextmanager
def admission_lock(root, *, blocking=True):
    path = Path(root).resolve() / '.pipeline/submission-admission.lock'
    held = getattr(_local, 'held', None)
    if held is None:
        held = _local.held = set()
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)


# Each request keeps the whole batch manifest in these, megabytes a row. Reading them on every worker pass took 750 MB
# for 324 open requests, and earlyoom stopped the worker (2026-11-17). Callers read the request's own columns only.
_LARGE = frozenset({'preview', 'pinned'})


def reservations(root, *, scope=None):
    if not isinstance(root, (str, Path)):
        return []
    path = Path(root) / '.pipeline/dashboard-submissions.sqlite3'
    if not path.is_file():
        return []
    with closing(read_only(path.resolve())) as db:
        db.row_factory = sqlite3.Row
        columns = ','.join(f'"{c[1]}"' for c in db.execute('PRAGMA table_info(requests)') if c[1] not in _LARGE)
        rows = db.execute(f"""SELECT {columns}, json_extract(preview,'$.scope') AS "scope" FROM requests WHERE
            state IN ('preparing','unknown','queued','running','sync_pending')
            OR (state IN ('blocked','turn_ended') AND prompt IS NOT NULL)""").fetchall()
    idle = _idle_appliers(Path(root))
    found = []
    for row in map(dict, rows):
        if row.pop('scope') == str(scope) or scope is None:
            row['applier_idle'] = row.get('owner') in idle
            found.append(row)
    return found


# A Claude Applier that is parked or has ended clicks nothing until something wakes it, and a woken one counts as running.
_IDLE_LAUNCHES = ('parked', 'finished', 'failed', 'stopped')


def _idle_appliers(root):
    path = root / '.pipeline/applier-runtime.sqlite3'
    if not path.is_file():
        return set()
    with closing(read_only(path.resolve())) as db:
        try:
            return {row[0] for row in db.execute(f"SELECT launch_id FROM applier_launches WHERE state IN ({','.join('?' * len(_IDLE_LAUNCHES))})",
                                                 _IDLE_LAUNCHES)}
        except sqlite3.OperationalError:
            return set()


def assert_available(root, job):
    if any(row['job'] == int(job) for row in reservations(root)):
        raise ValueError('Job reserved by an exact dashboard submission confirmation.')


def occupies_slot(row):
    """Whether the confirmation holds an application slot. One whose Applier is parked or has ended holds none: on
    2026-11-19 four parked Appliers waiting on the user held all four slots, and no job started for a day."""
    return row['state'] != 'sync_pending' and row['prompt'] is not None and not row.get('terminal_turn') and not row.get('applier_idle')
