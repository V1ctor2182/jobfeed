"""Helpers for the pipeline's private local files and databases under the runtime root."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z')


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f'{field} must be an opaque identifier')
    return value


def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def confined(root, path):
    root, path = Path(root).resolve(), Path(path)
    if not path.is_absolute() or not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError('artifact path escapes registered repository')
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError('artifact path contains a symbolic link')
        current = current.parent
    return path


def private_json(path, value, root=None):
    path = confined(root, path) if root is not None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(dump(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
