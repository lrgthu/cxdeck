"""Private, atomic workbench metadata. Never read or modify Codex transcripts."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid


class StateError(RuntimeError):
    pass


def name(value, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StateError(f"Name must contain 1–{maximum} characters.")
    if any(not c.isprintable() for c in value):
        raise StateError("Names cannot contain terminal control characters.")
    return value.strip()


def digest(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


def live_key(row, context):
    if context.get('backend') != 'zmx' or row.get('backend') != 'zmx':
        raise StateError('Only zmx live generations are supported.')
    generation = row.get('generation') or {}
    return digest([context['host'], 'zmx',
        os.path.realpath(generation.get('runtime_dir') or context['runtime_dir']),
        generation.get('session') or row['session'],
        int(generation.get('daemon_pid') or row['daemon_pid']),
        int(generation.get('created') or row['created'])])


def thread_key(home, tid, host):
    return digest([host, os.path.realpath(home), 'thread', tid]) if home and tid else None


class Store:
    def __init__(self, root=None):
        if root is None:
            from cx_paths import state_home
            self.root = state_home() / 'workbench'
        else:
            self.root = Path(root)
        self.path = self.root / 'state.json'

    def _validate_path(self):
        """Validate an existing Store path without mutating filesystem metadata."""
        # The default layout has four user-controlled components below HOME
        # (``.local/state/cxdeck/workbench``). Refuse a symlink at any of them;
        # checking only the leaf would still permit writes through a symlinked
        # ``.local`` or ``state`` directory.
        for p in (self.root, *list(self.root.parents)[:3]):
            if p.is_symlink():
                raise StateError(f"State path contains a symlink: {p}")
        if self.root.exists() and (not self.root.is_dir() or self.root.stat().st_uid != os.getuid()):
            raise StateError("State directory has another owner.")

    def prepare(self):
        # Refuse symlinked parents instead of chmod-ing or writing through them.
        self._validate_path()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.stat().st_uid != os.getuid():
            raise StateError("State directory has another owner.")
        os.chmod(self.root, 0o700)

    @contextmanager
    def lock(self, purpose='state'):
        self.prepare()
        fd = os.open(self.root / (purpose + '.lock'),
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
                raise StateError("Unsafe state lock.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError(f"Another workbench {purpose} operation is running; retry.") from exc
            yield
        finally:
            os.close(fd)

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return dict(version=1, agents={}, views={}, workspaces={}, groups={}, config={})
        self._validate_path()
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4 * 1024 * 1024:
                raise StateError("Unsafe or oversized workbench state file.")
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                fd = None
                data = json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise StateError("Invalid state.json; restore a backup instead of overwriting it.") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(data, dict) or data.get('version') != 1:
            raise StateError("Unsupported workbench state version.")
        for key in ('agents', 'views', 'workspaces'):
            if not isinstance(data.get(key), dict) or any(not isinstance(v, dict) for v in data[key].values()):
                raise StateError(f"Invalid workbench {key} metadata.")
        if 'groups' in data and (not isinstance(data['groups'], dict) or
                any(not isinstance(v, dict) for v in data['groups'].values())):
            raise StateError("Invalid workbench groups metadata.")
        data.setdefault('groups', {})
        if 'config' in data and not isinstance(data['config'], dict):
            raise StateError("Invalid CX Deck configuration metadata.")
        data.setdefault('config', {})
        return data

    def change(self, operation):
        with self.lock():
            data = self.read()
            result = operation(data)
            payload = (json.dumps(data, indent=2, ensure_ascii=True) + '\n').encode()
            if len(payload) > 4 * 1024 * 1024:
                raise StateError("Workbench metadata exceeds 4 MiB; nothing written.")
            fd, tmp = tempfile.mkstemp(prefix='.state-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, 0o600)
                if self.path.exists():
                    # Keep one last-known parseable state for manual recovery.
                    backup = self.root / 'state.previous.json'
                    if backup.is_symlink():
                        raise StateError("Backup path is a symlink.")
                    old = self.path.read_bytes()
                    bfd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                                  getattr(os, 'O_NOFOLLOW', 0), 0o600)
                    with os.fdopen(bfd, 'wb') as stream:
                        stream.write(old)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return result

    def annotate(self, keys, **values):
        if not any(keys):
            return
        def edit(data):
            for key in keys:
                if key:
                    data['agents'].setdefault(key, {}).update(values)
        self.change(edit)

    def preference(self, key, default=None):
        if key != 'timestamps':
            raise StateError("Unsupported CX Deck preference.")
        value = self.read()['config'].get(key, default)
        if not isinstance(value, bool):
            raise StateError("Invalid CX Deck timestamp preference.")
        return value

    def set_preference(self, key, value):
        if key != 'timestamps' or not isinstance(value, bool):
            raise StateError("Unsupported CX Deck preference.")
        self.change(lambda data: data['config'].__setitem__(key, value))

    def workspace(self, title, record, replace=False):
        title = name(title, 80)
        def edit(data):
            if title in data['workspaces'] and not replace:
                raise StateError("Workspace exists. Use --replace deliberately; nothing overwritten.")
            data['workspaces'][title] = copy.deepcopy(record)
        self.change(edit)

    def set_workspace_layout(self, title, layout, expected_workspace, expected_views,
                             replace=False):
        """Atomically add/replace an already validated exact presentation layout."""
        from cx_workspace_layout import validate_layout
        title = name(title, 80)
        validated = validate_layout(layout)
        expected_workspace = copy.deepcopy(expected_workspace)
        expected_views = copy.deepcopy(expected_views)
        def edit(data):
            current = data['workspaces'].get(title)
            if current is None:
                raise StateError('Workspace disappeared before exact layout commit; nothing written.')
            if current != expected_workspace:
                raise StateError('Workspace changed during exact layout capture; nothing written.')
            if data['views'] != expected_views:
                raise StateError('Verified view metadata changed during exact layout capture; nothing written.')
            if current.get('exact_layout') is not None and not replace:
                raise StateError('Workspace already has an exact layout. Use --replace deliberately; nothing overwritten.')
            updated = copy.deepcopy(current)
            updated['exact_layout'] = validated
            data['workspaces'][title] = updated
        self.change(edit)

    def set_view_receipts(self, receipts):
        """Commit independently verified preferred-view receipts in one update."""
        if not isinstance(receipts, dict):
            raise StateError('Invalid preferred-view receipt batch.')
        checked = {}
        for key, receipt in receipts.items():
            if (not isinstance(key, str) or not key or not isinstance(receipt, dict) or
                    set(receipt) != {'guid', 'tty'} or
                    not isinstance(receipt['guid'], str) or not receipt['guid'] or
                    not isinstance(receipt['tty'], str) or not receipt['tty'].startswith('/dev/')):
                raise StateError('Invalid preferred-view receipt batch.')
            checked[key] = copy.deepcopy(receipt)
        def edit(data):
            data['views'].update(checked)
        self.change(edit)

    def resolve_group(self, token):
        groups = self.read()['groups']
        exact = [ident for ident in groups if ident == token]
        matches = exact or [ident for ident, record in groups.items() if record.get('name') == token]
        if len(matches) != 1:
            raise StateError("Group ID/name must match exactly and unambiguously.")
        return matches[0], groups[matches[0]]

    def create_group(self, title):
        title = name(title, 80)
        ident = 'group-' + str(uuid.uuid4())
        def edit(data):
            if any(record.get('name') == title for record in data['groups'].values()):
                raise StateError("A group with that exact name already exists.")
            order = max((record.get('order', 0) for record in data['groups'].values()), default=-1) + 1
            data['groups'][ident] = dict(name=title, created_at=time.time(), order=order, collapsed=False)
        self.change(edit)
        return ident

    def rename_group(self, token, title):
        title = name(title, 80)
        ident, _ = self.resolve_group(token)
        def edit(data):
            if any(key != ident and record.get('name') == title for key, record in data['groups'].items()):
                raise StateError("A group with that exact name already exists.")
            data['groups'][ident]['name'] = title
        self.change(edit)

    def delete_group(self, token):
        ident, _ = self.resolve_group(token)
        def edit(data):
            del data['groups'][ident]
            for metadata in data['agents'].values():
                if metadata.get('group_id') == ident:
                    metadata.pop('group_id')
        self.change(edit)

    def assign_group(self, agent_keys, group_id=None):
        if group_id is not None and group_id not in self.read()['groups']:
            raise StateError("Unknown group ID.")
        def edit(data):
            for key in agent_keys:
                if not key:
                    raise StateError("Group membership requires an exact conversation UUID.")
                metadata = data['agents'].setdefault(key, {})
                if group_id is None:
                    metadata.pop('group_id', None)
                else:
                    metadata['group_id'] = group_id
        self.change(edit)
