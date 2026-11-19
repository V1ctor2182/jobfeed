"""Private, versioned short answers. Approval applies to one exact current revision."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pipeline.common_questions import CATALOG

ROOT = Path(__file__).resolve().parents[1]
STATES = {'pending_review', 'approved', 'revoked', 'superseded'}
CONTENT = ('question', 'answer', 'aliases', 'scope', 'valid_until')
# An answer is approved in the dashboard, or by the user's own reply to the question an Applier asked, or was decided by an
# Applier itself while the user's auto submit mode is on. The user's own answers outrank an Applier's and are never replaced by one.
APPLIER_AUTO = 'applier_auto'
APPROVAL_SOURCES = {'dashboard', 'user_reply', APPLIER_AUTO}
# The narrowest scope wins when answers at several scopes match, and the newest approval among equals.
SCOPE_RANK = {'job': 0, 'company': 1, 'global': 2}


class BankError(ValueError):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.code = code


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def normalize_question(value):
    text = unicodedata.normalize('NFKC', value).lower()
    text = re.sub(r'\s*\((?:required|optional)\)\s*$', '', text)
    return re.sub(r'\s+', ' ', text).strip().rstrip('?.: *').strip()


_COMMON = {normalize_question(wording): item for item in CATALOG for wording in (item.question, *item.aliases)}
_BY_KEY = {item.key: item for item in CATALOG}


# Contact and identity facts live in the application profile, which lookup_profile reads. A reply never overrides them.
PROFILE_FIELDS = frozenset(map(normalize_question, (
    'Name', 'First name', 'Last name', 'Full name', 'Legal name', 'Preferred name', 'Email', 'Email address', 'E-mail',
    'Phone', 'Phone number', 'Mobile phone', 'Address', 'Street address', 'City', 'State', 'Zip code', 'Postal code',
    'LinkedIn', 'LinkedIn profile', 'LinkedIn URL', 'GitHub', 'Portfolio', 'Website', 'Personal website')))


def common_question(question):
    """The catalog item that this question is a wording of, if any."""
    return _COMMON.get(normalize_question(question))


def _wordings(version):
    return [version['question'], *version['aliases']]


def _with_common_wordings(draft):
    """A draft for a common question also answers every other wording of it."""
    item = next(filter(None, map(common_question, _wordings(draft))), None)
    if not item:
        return draft
    asked = normalize_question(draft['question'])
    extra = [w for w in (item.question, *item.aliases) if normalize_question(w) != asked]
    return content(dict(draft, aliases=[*draft['aliases'], *extra]))


def applier_decided(version, job_id=None):
    """Whether this version is an Applier's own answer, for job_id when given, that the user has not since changed."""
    source = (version.get('approval') or {}).get('source', {})
    return version['state'] == 'approved' and source.get('kind') == APPLIER_AUTO and (job_id is None or source.get('job_id') == str(job_id))


def _approve_new_version(entry, draft, *, event_id, source):
    """Append draft to entry as its approved current version. Saving it is the approval."""
    number = entry['current_version'] + 1
    now = timestamp()
    if entry['versions']:
        current = entry['versions'][-1]
        current['superseded_state'], current['state'], current['superseded_at'] = current['state'], 'superseded', now
    entry['current_version'] = number
    entry['versions'].append(dict(draft, version=number, state='approved', created_at=now,
                                  approval={'version': number, 'approved_at': now, 'event_id': event_id, 'source': source}))
    return entry


def common_questions(store):
    """The catalog, each item with the approved global answer the Dashboard shows for it."""
    view = []
    for item in CATALOG:
        answered = [entry for entry in store['entries'] if entry['versions'][-1]['scope']['kind'] == 'global'
                    and entry['versions'][-1]['state'] == 'approved'
                    and any(common_question(q) is item for q in _wordings(entry['versions'][-1]))]
        latest = max(answered, key=lambda entry: entry['versions'][-1]['approval']['approved_at'], default=None)
        view.append({'key': item.key, 'group': item.group, 'question': item.question, 'aliases': list(item.aliases),
                     'options': list(item.options), 'entry_id': latest and latest['id']})
    return view


_STOPWORDS = frozenset('a an and any are at be can do does for have i in is it of on or the this to will with you your'.split())


_UNITED_STATES = re.compile(r'\bu\.s\.?(?:a\.?)?(?=\W|$)|\busa\b')


def _words(question):
    text = _UNITED_STATES.sub(' united states ', normalize_question(question))
    return set(re.findall(r'[a-z0-9]+', text)) - _STOPWORDS


def _date(value):
    if not isinstance(value, str):
        raise BankError('Expiry must be an ISO date or timestamp.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise BankError('Expiry must be an ISO date or timestamp.') from exc
    if parsed.tzinfo is None:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise BankError('Expiry timestamps must include a timezone.')
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def content(value):
    if not isinstance(value, dict):
        raise BankError('A complete question and answer draft is required.')
    if set(value) - set(CONTENT):
        raise BankError('Draft contains unsupported fields.')
    for key in ('question', 'answer'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise BankError(f'{key} must be nonempty text.')
    aliases = value.get('aliases', [])
    if not isinstance(aliases, list) or not all(isinstance(x, str) and x.strip() for x in aliases):
        raise BankError('Equivalent questions must be nonempty text.')
    scope = value.get('scope')
    if not isinstance(scope, dict) or scope.get('kind') not in {'global', 'company', 'job'}:
        raise BankError('Choose global, company, or job scope.')
    allowed = {'global': {'kind'}, 'company': {'kind', 'company'}, 'job': {'kind', 'company', 'job_id'}}[scope['kind']]
    if set(scope) - allowed:
        raise BankError('Scope contains unsupported fields.')
    scope = copy.deepcopy(scope)
    if scope['kind'] != 'global' and (not isinstance(scope.get('company'), str) or not scope['company'].strip()):
        raise BankError('Company is required for company and job scope.')
    if scope['kind'] == 'job':
        if isinstance(scope.get('job_id'), bool) or not isinstance(scope.get('job_id'), (str, int)) or not str(scope['job_id']).strip():
            raise BankError('Job ID is required for job scope.')
        scope['job_id'] = str(scope['job_id'])
    expiry = value.get('valid_until') or None
    if expiry is not None:
        _date(expiry)
    return {'question': value['question'], 'answer': value['answer'], 'aliases': list(dict.fromkeys(aliases)), 'scope': scope, 'valid_until': expiry}


def validate(store):
    if not isinstance(store, dict) or type(store.get('schema_version')) is not int or store.get('schema_version') != 1 or type(store.get('revision')) is not int or store['revision'] < 0 or not isinstance(store.get('entries'), list):
        raise BankError('The answer bank is malformed.', 'unavailable')
    ids = set()
    for entry in store['entries']:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not entry['id'] or entry['id'] in ids:
            raise BankError('The answer bank has invalid entry IDs.', 'unavailable')
        ids.add(entry['id'])
        versions = entry.get('versions')
        if type(entry.get('current_version')) is not int or not isinstance(versions, list) or not versions or any(not isinstance(v, dict) or type(v.get('version')) is not int for v in versions) or [v.get('version') for v in versions if isinstance(v, dict)] != list(range(1, len(versions) + 1)) or entry.get('current_version') != len(versions):
            raise BankError('The answer bank has invalid versions.', 'unavailable')
        for version in versions:
            if not all(k in version for k in CONTENT):
                raise BankError('The answer bank has incomplete content.', 'unavailable')
            try:
                canonical = content({k: version[k] for k in CONTENT})
            except BankError as exc:
                raise BankError('The answer bank has invalid content.', 'unavailable') from exc
            if canonical != {k: version[k] for k in CONTENT}:
                raise BankError('The answer bank has noncanonical content.', 'unavailable')
            if version.get('state') not in STATES or not isinstance(version.get('created_at'), str):
                raise BankError('The answer bank has invalid review state.', 'unavailable')
            if version['state'] == 'approved':
                approval = version.get('approval')
                if not isinstance(approval, dict) or approval.get('version') != version['version'] or not approval.get('approved_at') or not isinstance(approval.get('source'), dict) or approval['source'].get('kind') not in APPROVAL_SOURCES or not isinstance(approval.get('event_id'), str) or not approval['event_id'].strip():
                    raise BankError('The answer bank has invalid approval provenance.', 'unavailable')
                try:
                    _date(approval['approved_at'])
                except BankError as exc:
                    raise BankError('The answer bank has invalid approval timestamp.', 'unavailable') from exc
    return store


class AnswerBank:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.path = self.root / '.pipeline/answer-bank.json'

    def read(self, *, missing_ok=True):
        try:
            return validate(json.loads(self.path.read_text()))
        except FileNotFoundError:
            if missing_ok:
                return {'schema_version': 1, 'revision': 0, 'entries': []}
            raise BankError('The answer bank is missing.', 'unavailable') from None
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BankError('The answer bank could not be read.', 'unavailable') from exc

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _write(self, store):
        validate(store)
        fd, name = tempfile.mkstemp(prefix='.answer-bank-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(store, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def mutate(self, request, *, dashboard=False):
        if not isinstance(request, dict) or set(request) - {'action', 'expected_revision', 'entry_id', 'expected_version', 'draft'}:
            raise BankError('Unsupported command fields.')
        action = request.get('action')
        if action not in {'create', 'edit', 'approve', 'revoke', 'save'}:
            raise BankError('Unknown answer bank action.')
        if action in {'approve', 'save'} and not dashboard:
            raise BankError('Approve the exact version in the local dashboard.')
        with self.locked():
            store = self.read()
            entry = next((x for x in store['entries'] if x['id'] == request.get('entry_id')), None)
            version = entry['versions'][-1] if entry else None
            draft = content(request.get('draft')) if action in {'create', 'edit', 'save'} else None
            if action == 'save':
                draft = _with_common_wordings(draft)
                if version and version['state'] == 'approved' and draft == {k: version[k] for k in CONTENT}:
                    return store
            # A repeated exact command converges without changing an intervening version.
            if action == 'create' and version and draft == {k: version[k] for k in CONTENT}:
                return store
            if action == 'edit' and version and request.get('expected_version') == version['version'] - 1 and version['state'] == 'pending_review' and draft == {k: version[k] for k in CONTENT}:
                return store
            if version and request.get('expected_version') == version['version']:
                if action == 'approve' and version['state'] == 'approved':
                    return store
                if action == 'revoke' and version['state'] == 'revoked':
                    return store
                if action == 'edit' and version['state'] == 'pending_review' and draft == {k: version[k] for k in CONTENT}:
                    return store
            if type(request.get('expected_revision')) is not int or request['expected_revision'] != store['revision']:
                raise BankError('The answer bank changed. Reload before saving.', 'conflict')
            if action == 'save':
                if draft['valid_until'] and _date(draft['valid_until']) <= datetime.now(timezone.utc):
                    raise BankError('This answer has already expired. Choose a later expiry.')
                if entry and request.get('expected_version') != version['version'] or not entry and request.get('expected_version') is not None:
                    raise BankError('This answer changed. Reload its current version.', 'conflict')
                if not entry:
                    identifier = request.get('entry_id') or str(uuid4())
                    if not isinstance(identifier, str) or not identifier.strip():
                        raise BankError('Entry ID is invalid.')
                    entry = {'id': identifier, 'current_version': 0, 'versions': []}
                    store['entries'].append(entry)
                _approve_new_version(entry, draft, event_id=str(uuid4()), source={'kind': 'dashboard', 'actor': 'local-user', 'action': 'save'})
            elif action == 'create':
                identifier = request.get('entry_id') or str(uuid4())
                if not isinstance(identifier, str) or not identifier.strip() or entry:
                    raise BankError('Entry ID already exists or is invalid.', 'conflict')
                version = dict(draft, version=1, state='pending_review', created_at=timestamp(), approval=None)
                store['entries'].append({'id': identifier, 'current_version': 1, 'versions': [version]})
            else:
                if not entry or type(request.get('expected_version')) is not int or request['expected_version'] != version['version']:
                    raise BankError('This answer changed. Reload its current version.', 'conflict')
                if action == 'edit':
                    previous_state = version['state']
                    version['state'] = 'superseded'
                    version['superseded_state'] = previous_state
                    version['superseded_at'] = timestamp()
                    entry['current_version'] += 1
                    entry['versions'].append(dict(draft, version=entry['current_version'], state='pending_review', created_at=timestamp(), approval=None))
                elif action == 'approve':
                    if version['state'] != 'pending_review':
                        raise BankError('Only a pending draft can be approved. Save a new draft first.')
                    if version['valid_until'] and _date(version['valid_until']) <= datetime.now(timezone.utc):
                        raise BankError('This answer has expired. Save a new draft first.')
                    version['state'] = 'approved'
                    version['approval'] = {'version': version['version'], 'approved_at': timestamp(), 'event_id': str(uuid4()), 'source': {'kind': 'dashboard', 'actor': 'local-user'}}
                else:
                    version['state'] = 'revoked'
                    version['revoked_at'] = timestamp()
            store['revision'] += 1
            self._write(store)
            return store

    def resolve(self, question, context, *, common_key=None, answer=None, char_limit=None, word_limit=None):
        """The approved answer to question for this job. common_key names the common question it asks, when the caller knows."""
        store = self.read(missing_ok=False)
        if not isinstance(context, dict) or not context.get('company') or not context.get('job_id'):
            return {'allowed': False, 'reason': 'answer_approval_context_missing'}
        asked, item = normalize_question(question), _BY_KEY[common_key] if common_key else common_question(question)
        matches = []
        for entry in store['entries']:
            current = entry['versions'][-1]
            if current['state'] != 'approved':
                continue
            wordings = {normalize_question(q) for q in _wordings(current)}
            # Another wording of the same common question matches too, but an exact wording is preferred.
            exact = asked in wordings
            if not exact and not (item and any(_COMMON.get(w) is item for w in wordings)):
                continue
            scope = current['scope']
            if scope['kind'] != 'global' and scope['company'].strip().casefold() != str(context['company']).strip().casefold():
                continue
            if scope['kind'] == 'job' and str(scope['job_id']) != str(context['job_id']):
                continue
            if current['valid_until'] and _date(current['valid_until']) <= datetime.now(timezone.utc):
                continue
            if answer is not None and answer != current['answer']:
                continue
            if char_limit is not None and len(current['answer']) > char_limit:
                continue
            if word_limit is not None and len(current['answer'].split()) > word_limit:
                continue
            matches.append(((applier_decided(current), SCOPE_RANK[scope['kind']], not exact), current['approval']['approved_at'], {'allowed': True, 'entry_id': entry['id'], 'version': current['version'], 'answer': current['answer'], 'question': question}))
        if not matches:
            return {'allowed': False, 'reason': 'answer_approval_required'}
        best = min(rank for rank, _, _ in matches)
        return max((match for match in matches if match[0] == best), key=lambda match: _date(match[1]))[2]

    def related(self, question, context, limit=8):
        """Approved answers in reach of this job whose questions share the most words with this one, for the Applier to judge."""
        words = _words(question)
        found = []
        for entry in self.read()['entries']:
            current = entry['versions'][-1]
            scope = current['scope']
            if current['state'] != 'approved' or (current['valid_until'] and _date(current['valid_until']) <= datetime.now(timezone.utc)):
                continue
            if scope['kind'] != 'global' and scope['company'].strip().casefold() != str(context['company']).strip().casefold():
                continue
            if scope['kind'] == 'job' and str(scope['job_id']) != str(context['job_id']):
                continue
            shared = max(len(words & _words(q)) for q in [current['question'], *current['aliases']])
            if shared >= 2:
                found.append((shared, {'question': current['question'], 'answer': current['answer'], 'scope': scope['kind']}))
        return [match for _, match in sorted(found, key=lambda pair: -pair[0])[:limit]]

    def learn(self, question, answer, scope, *, request_id, reply_version, channel):
        """Save the user's reply to an Applier's question as an approved answer. The reply is the approval.

        A later reply to the same question at the same scope replaces the earlier answer.
        """
        return self._save(question, answer, scope, event_id=f'{request_id}:{reply_version}',
                          source={'kind': 'user_reply', 'channel': channel, 'request_id': request_id})

    def decide(self, question, answer, scope, context, *, owner, common_key=None):
        """Save an answer that the Applier for context's job decided itself under the user's auto submit mode, so later Appliers reuse it.

        It never replaces or outranks the user's own answer: when one applies to this job, or the user changed or deleted the
        answer at this scope, it raises BankError with code 'user_answer'.
        """
        # The user's answers rank first, so the best match is the user's whenever any applies.
        found = self.resolve(question, context, common_key=common_key) if self.path.exists() else {'allowed': False}
        if found['allowed'] and not applier_decided(next(e for e in self.read()['entries'] if e['id'] == found['entry_id'])['versions'][-1]):
            raise BankError(f"The user already answered this question. Fill their answer: {found['answer']}", 'user_answer')
        return self._save(question, answer, scope, event_id=str(uuid4()),
                          source={'kind': APPLIER_AUTO, 'job_id': str(context['job_id']), 'owner': owner})

    def _save(self, question, answer, scope, *, event_id, source):
        draft = content({'question': question, 'answer': answer, 'scope': scope})
        key = normalize_question(question)
        if key in PROFILE_FIELDS:
            raise BankError(f'{question} is a profile fact. It comes from the application profile and is never saved as an answer.')
        with self.locked():
            store = self.read()
            entry = next((e for e in store['entries'] if e['versions'][-1]['scope'] == draft['scope']
                          and key in {normalize_question(q) for q in [e['versions'][-1]['question'], *e['versions'][-1]['aliases']]}), None)
            current = entry['versions'][-1] if entry else None
            if current and source['kind'] == APPLIER_AUTO and not applier_decided(current):
                raise BankError('The user changed or deleted this answer in My Profile, so an Applier never saves over it. Nothing was saved.', 'user_answer')
            if current and current['state'] == 'approved' and current['answer'] == draft['answer']:
                return entry
            if not entry:
                entry = {'id': str(uuid4()), 'current_version': 0, 'versions': []}
                store['entries'].append(entry)
            _approve_new_version(entry, dict(draft, aliases=current['aliases'] if current else []), event_id=event_id, source=source)
            store['revision'] += 1
            self._write(store)
            return entry

    def pending(self, question, answer, context):
        draft = content({'question': question, 'answer': answer, 'scope': {'kind': 'job', 'company': context['company'], 'job_id': str(context['job_id'])}})
        for attempt in range(3):
            store = self.read()
            for entry in store['entries']:
                current = entry['versions'][-1]
                if {k: current[k] for k in CONTENT} == draft:
                    return entry
            try:
                updated = self.mutate({'action': 'create', 'expected_revision': store['revision'], 'draft': draft})
                return updated['entries'][-1]
            except BankError as exc:
                if exc.code != 'conflict' or attempt == 2:
                    raise

    def propose(self, application_folder, question, answer):
        folder = Path(application_folder).resolve()
        if not folder.is_relative_to(self.root / 'applications'):
            raise BankError('Application folder is outside this workspace.')
        task = json.loads((folder / 'application-task.json').read_text())
        if not task.get('company') or not task.get('job_id'):
            raise BankError('Application context is missing.')
        return self.pending(question, answer, {'company': task['company'], 'job_id': str(task['job_id'])})

    def import_review(self, review, *, scopes=None):
        """Import legacy drafts and rejection history. Never import approval claims."""
        with self.locked():
            store = self.read()
            changed = False
            for row in review['entries']:
                identifier = row['id']
                history = sorted([x for x in review.get('history', []) if x['id'] == identifier] + [row], key=lambda x: x['version'])
                existing = next((e for e in store['entries'] if e['id'] == identifier), None)
                if existing:
                    # Re-import cannot overwrite later human edits or approval.
                    continue
                scope = (scopes or {}).get(identifier, {'kind': 'job', 'company': review['company'], 'job_id': str(review['job_id'])})
                versions = []
                for index, old in enumerate(history, 1):
                    data = content({'question': old['question'], 'answer': old['answer'], 'scope': scope})
                    versions.append(dict(data, version=index, state='pending_review' if old is row else 'superseded', created_at=timestamp(), approval=None, imported_status=old.get('status'), review_decision=old.get('review_decision') or old.get('revision_requested_by'), source={'kind': 'legacy_review', 'job_id': str(review['job_id'])}))
                store['entries'].append({'id': identifier, 'current_version': len(versions), 'versions': versions})
                changed = True
            if changed:
                store['revision'] += 1
                self._write(store)
            return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('command', choices=['list', 'mutate', 'dashboard-mutate', 'resolve', 'import-review', 'propose'])
    args = parser.parse_args()
    try:
        bank = AnswerBank(args.root)
        request = json.load(sys.stdin)
        if args.command == 'list':
            result = bank.read()
        elif args.command in {'mutate', 'dashboard-mutate'}:
            result = bank.mutate(request, dashboard=args.command == 'dashboard-mutate')
        if args.command in {'list', 'mutate', 'dashboard-mutate'}:
            result = dict(result, common_questions=common_questions(result))
        elif args.command == 'propose':
            result = bank.propose(**request)
        elif args.command == 'import-review':
            result = bank.import_review(json.loads(Path(request['path']).read_text()), scopes=request.get('scopes'))
        else:
            result = bank.resolve(**request)
        print(json.dumps(result, ensure_ascii=False))
    except (BankError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc), 'code': getattr(exc, 'code', 'invalid')}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
