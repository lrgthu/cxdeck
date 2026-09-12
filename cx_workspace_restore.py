"""Safe exact native-iTerm reconstruction for workspace-layout/v1.

Runtime planning is pure. The iTerm2 package is imported only by the explicit
exact-restore feature. Existing verified Sessions are moved in place; Codex and
zmx generations are never restarted, replaced, detached, or relabeled here.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from dataclasses import dataclass
import importlib.metadata
import io
import os
from pathlib import Path
import socket
import time

import cx_iterm
from cx_store import StateError, name as presentation_name
from cx_workspace_layout import (ITermPythonReader, LayoutError, _bind_snapshot,
                                 _validate_raw_snapshot, conversation_identities,
                                 validate_layout)


POLICY = 'MOVE_REUSE'


class RestoreError(RuntimeError):
    def __init__(self, state, message):
        self.state = state
        super().__init__(f'{state}: {message}')


@dataclass(frozen=True)
class RuntimeItem:
    identity: tuple
    classification: str
    row: dict
    expected_generation: object
    expected_pids: tuple
    display_name: str
    cwd: str | None


@dataclass(frozen=True)
class RuntimePlan:
    layout: dict
    workspace: dict
    items: tuple
    cold_count: int


@dataclass(frozen=True)
class RestoreResult:
    state: str
    existing_views_reused: int
    existing_views_moved: int
    existing_views_rebuilt: int
    missing_views_created: int
    geometry_limitations: tuple
    view_receipts: dict


def _generation(row, context):
    if not isinstance(row, dict):
        raise RestoreError('RUNTIME_BLOCKED', 'Managed conversation has an invalid zmx record.')
    generation = row.get('generation')
    keys = ('host', 'runtime_dir', 'session', 'daemon_pid', 'created')
    if (row.get('backend') != 'zmx' or not isinstance(generation, dict) or
            any(key not in generation for key in keys)):
        raise RestoreError('RUNTIME_BLOCKED', 'Managed conversation has an incomplete zmx generation.')
    try:
        result = (generation['host'], os.path.realpath(generation['runtime_dir']),
                  generation['session'], int(generation['daemon_pid']), int(generation['created']))
    except (TypeError, ValueError) as exc:
        raise RestoreError('RUNTIME_BLOCKED', 'Managed conversation has an invalid zmx generation.') from exc
    expected = (context.get('host'), os.path.realpath(context.get('runtime_dir') or ''),
                row.get('session'), row.get('daemon_pid'), row.get('created'))
    if result != expected or result[3] <= 0 or result[4] <= 0:
        raise RestoreError('RUNTIME_BLOCKED', 'Managed conversation generation disagrees with current runtime facts.')
    return result


def _workspace_identities(workspace):
    if not isinstance(workspace, dict) or not isinstance(workspace.get('members'), list):
        raise RestoreError('RUNTIME_BLOCKED', 'Workspace membership metadata is invalid.')
    host = workspace.get('host')
    identities = []
    for member in workspace['members']:
        if not isinstance(member, dict) or not member.get('thread_id') or not member.get('home'):
            raise RestoreError('RUNTIME_BLOCKED', 'Exact restore requires UUID-backed workspace members.')
        identities.append((host, os.path.realpath(member['home']), member['thread_id']))
    return identities


def structural_layout(layout):
    """Return exact grouping/tree, excluding L3 hints and window stacking order."""
    validated = validate_layout(layout)
    def node(value):
        if value['type'] == 'conversation':
            return {'type': 'conversation', 'identity': copy.deepcopy(value['identity'])}
        return {'type': 'split', 'axis': value['axis'],
                'children': [node(child) for child in value['children']]}
    windows = [{'tabs': [{'root': node(tab['root'])} for tab in window['tabs']]}
               for window in validated['windows']]
    # Supported iTerm APIs can move tabs between windows but do not expose a
    # stable window-stacking reorder operation. Window grouping is exact;
    # stacking/list order is presentation geometry and intentionally ignored.
    windows.sort(key=lambda value: repr(value))
    return {'windows': windows}


def first_identity(node):
    if node['type'] == 'conversation':
        ident = node['identity']
        return ident['host'], ident['codex_home'], ident['thread_id']
    return first_identity(node['children'][0])


def reconstruction_blueprint(layout):
    """Pure ordered anchors and moves sufficient to construct the exact tree."""
    validated = validate_layout(layout)
    windows, moves = [], []
    def visit(node):
        if node['type'] == 'conversation':
            return
        representatives = [first_identity(child) for child in node['children']]
        for previous, source in zip(representatives, representatives[1:]):
            moves.append({'source': source, 'destination': previous,
                          'axis': node['axis'],
                          # iTerm 3.7.0/iterm2 2.23 empirically puts a moved
                          # row child after its destination with before=True.
                          'before': node['axis'] == 'rows'})
        for child in node['children']:
            visit(child)
    for window in validated['windows']:
        tabs = []
        for tab in window['tabs']:
            anchor = first_identity(tab['root'])
            tabs.append({'anchor': anchor, 'root': tab['root']})
            visit(tab['root'])
        windows.append({'anchor': tabs[0]['anchor'], 'tabs': tabs,
                        'frame_hint': window.get('frame_hint')})
    return {'windows': windows, 'moves': moves}


def build_runtime_plan(layout, workspace, inventory_rows, unknown_pids, process_failed,
                       *, host, codex_home, allow_unverified_live=False,
                       cwd_override=None, planned_cwd=None, context=None):
    """Classify every durable leaf before any runtime or presentation action."""
    validated = validate_layout(layout)
    if workspace.get('host') != host:
        raise RestoreError('RUNTIME_BLOCKED', 'Workspace belongs to another host.')
    durable = conversation_identities(validated)
    members = _workspace_identities(workspace)
    if len(members) != len(set(members)) or set(members) != set(durable):
        raise RestoreError('RUNTIME_BLOCKED', 'Exact layout leaves do not equal workspace membership.')
    home = os.path.realpath(codex_home)
    if any(identity[0] != host or identity[1] != home for identity in durable):
        raise RestoreError('RUNTIME_BLOCKED', 'Exact restore supports only this host and active CODEX_HOME.')
    context = context or {'host': host, 'runtime_dir': ''}
    by_identity = {}
    for row in inventory_rows:
        tid, row_home = row.get('thread_id'), row.get('home')
        if not tid or not row_home:
            continue
        identity = (host, os.path.realpath(row_home), tid)
        by_identity.setdefault(identity, []).append(row)
    items, cold = [], 0
    members_by_identity = {(host, os.path.realpath(item['home']), item['thread_id']): item
                           for item in workspace['members']}
    for identity in durable:
        candidates = by_identity.get(identity, [])
        managed = [row for row in candidates if row.get('managed') and
                   row['managed'].get('state') in ('ALIVE', 'STOPPED')]
        if len(managed) > 1:
            raise RestoreError('RUNTIME_BLOCKED', 'Two managed generations claim ' + identity[2] + '.')
        candidate = managed[0] if managed else candidates[0] if len(candidates) == 1 else None
        if candidate is None:
            raise RestoreError('RUNTIME_BLOCKED', 'Conversation is absent from saved history and live runtime: ' + identity[2])
        if candidate.get('external_pids'):
            raise RestoreError('RUNTIME_BLOCKED', 'Known external same-UUID process blocks ' + identity[2] + '.')
        live = candidate.get('managed') if managed else None
        if candidate.get('managed') and live is None:
            raise RestoreError('RUNTIME_BLOCKED',
                               'Managed generation is not safely live for ' + identity[2] + '.')
        member = members_by_identity[identity]
        display = member.get('title') or candidate.get('title') or identity[2]
        try:
            display = presentation_name(display)
        except StateError as exc:
            raise RestoreError('RUNTIME_BLOCKED',
                               'Workspace contains an unsafe presentation name for ' + identity[2] + '.') from exc
        if live:
            generation = _generation(live, context)
            pids = tuple(live.get('codex_pids') or ())
            if not pids:
                raise RestoreError('RUNTIME_BLOCKED', 'Managed conversation has no verified Codex process: ' + identity[2])
            items.append(RuntimeItem(identity, 'LIVE_MANAGED', candidate, generation,
                                     pids, display, None))
        else:
            cold += 1
            row = dict(candidate)
            row['cwd'] = member.get('cwd') or candidate.get('cwd')
            cwd = planned_cwd(row, cwd_override) if planned_cwd else row.get('cwd')
            items.append(RuntimeItem(identity, 'SAVED_ONLY', row, None, (), display, cwd))
    if cold and (unknown_pids or process_failed) and not allow_unverified_live:
        detail = unknown_pids or 'UNKNOWN'
        raise RestoreError('RUNTIME_BLOCKED',
                           f'Unidentified live Codex PIDs {detail} could duplicate a saved-only resume.')
    return RuntimePlan(validated, copy.deepcopy(workspace), tuple(items), cold)


def workspace_for_layout(layout):
    return {'host': conversation_identities(layout)[0][0],
            'members': [{'home': home, 'thread_id': tid}
                        for _, home, tid in conversation_identities(layout)]}


def _raw_locations(raw):
    locations = {}
    def visit(node, window_id, tab_id):
        if node['type'] == 'session':
            locations[node['tty']] = {'guid': node['guid'], 'tty': node['tty'],
                                      'window_id': window_id, 'tab_id': tab_id}
            return
        for child in node['children']:
            visit(child, window_id, tab_id)
    for window in raw['windows']:
        for tab in window['tabs']:
            visit(tab['root'], window['window_id'], tab['tab_id'])
    return locations


def _runtime_rows(items, observation, *, allow_saved_missing=False):
    context = observation.get('context') or {}
    claims = {}
    for row in observation.get('sessions', []):
        if row.get('thread_id') and row.get('codex_home'):
            identity = ((row.get('generation') or {}).get('host'),
                        os.path.realpath(row['codex_home']), row['thread_id'])
            if row.get('state') in ('ALIVE', 'STOPPED'):
                claims.setdefault(identity, []).append(row)
    result = {}
    for item in items:
        matches = claims.get(item.identity, [])
        if not matches and allow_saved_missing and item.classification == 'SAVED_ONLY':
            continue
        if len(matches) != 1:
            raise RestoreError('RUNTIME_BLOCKED',
                               'Expected exactly one live managed generation for ' + item.identity[2] + '.')
        row = matches[0]
        generation = _generation(row, context)
        if item.expected_generation is not None and generation != item.expected_generation:
            raise RestoreError('RUNTIME_BLOCKED', 'zmx generation changed after restore planning.')
        if item.expected_pids and tuple(row.get('codex_pids') or ()) != item.expected_pids:
            raise RestoreError('RUNTIME_BLOCKED', 'Codex PID set changed after restore planning.')
        if row.get('external_pids'):
            raise RestoreError('RUNTIME_BLOCKED', 'A known external duplicate appeared after planning.')
        result[item.identity] = row
    if observation.get('duplicate_error'):
        raise RestoreError('RUNTIME_BLOCKED', str(observation['duplicate_error']))
    return result


def revalidate_runtimes(items, observation):
    """Require every planned leaf's exact generation and Codex PID set now."""
    return _runtime_rows(items, observation)


class PythonRestorer:
    def __init__(self, iterm2, connection, app, layout, items, runtime_reader,
                 context, caller_tty='', profile_name=cx_iterm.PROFILE_NAME):
        self.iterm2 = iterm2
        self.connection = connection
        self.app = app
        self.layout = validate_layout(layout)
        self.items = tuple(items)
        self.runtime_reader = runtime_reader
        self.context = context
        self.caller_tty = caller_tty
        self.profile_name = profile_name
        self.reader = ITermPythonReader(iterm2, app, importlib.metadata.version('iterm2'))
        self.rows = {}
        self.guids = {}
        self.initial_existing = set()
        self.moved = set()
        self.created = set()
        self.limitations = []

    async def _refresh(self):
        await self.app.async_refresh()

    async def _location(self, identity, *, allow_saved_missing=False,
                        expected_window=None, different_window=None, different_tab=None):
        deadline = time.monotonic() + 3
        guid = self.guids.get(identity)
        while time.monotonic() < deadline:
            await self._refresh()
            found = None
            for window in list(self.app.windows):
                for tab in list(window.tabs):
                    session = next((item for item in list(tab.sessions)
                                    if item.session_id == guid), None)
                    if session is not None:
                        found = (session, tab, window)
                        break
                if found:
                    break
            # Parent links can briefly be absent after a supported move. Always
            # re-resolve from materialized collections and scalar IDs.
            if found is None:
                await asyncio.sleep(0.05)
                continue
            session, tab, window = found
            if ((expected_window is not None and window.window_id != expected_window) or
                    (different_window is not None and window.window_id == different_window) or
                    (different_tab is not None and tab.tab_id == different_tab)):
                await asyncio.sleep(0.05)
                continue
            tty = await session.async_get_variable('tty')
            observation = self.runtime_reader()
            self.rows = _runtime_rows(self.items, observation,
                                      allow_saved_missing=allow_saved_missing)
            clients = observation['clients'].get(self.rows[identity]['sid'], [])
            if set(clients) != {tty}:
                raise RestoreError('PRESENTATION_CONFLICT', 'iTerm/zmx client binding changed during restore.')
            return session, tab, window
        raise RestoreError('PRESENTATION_CONFLICT',
                           'A verified iTerm Session disappeared or had no stable parent during restore.')

    async def _session(self, identity):
        return (await self._location(identity))[0]

    def _observe_rows(self):
        observation = self.runtime_reader()
        self.rows = _runtime_rows(self.items, observation)
        return observation

    async def _discover(self):
        observation = self.runtime_reader()
        self.rows = _runtime_rows(self.items, observation, allow_saved_missing=True)
        raw = await self.reader.read()
        _validate_raw_snapshot(raw)
        locations = _raw_locations(raw)
        for item in self.items:
            if item.identity not in self.rows:
                continue
            row = self.rows[item.identity]
            clients = observation['clients'].get(row['sid'], [])
            if row.get('attached', 0) > 1 or len(set(clients)) > 1:
                raise RestoreError('PRESENTATION_CONFLICT', 'Multiple clients make exact view placement ambiguous.')
            if row.get('attached') == 1 and len(set(clients)) == 1:
                tty = next(iter(clients))
                location = locations.get(tty)
                if location is None:
                    raise RestoreError('PRESENTATION_CONFLICT',
                                       'An attached zmx client is not a verifiable iTerm Session.')
                self.guids[item.identity] = location['guid']
                self.initial_existing.add(item.identity)
                _, tab, _ = await self._location(item.identity, allow_saved_missing=True)
                if tab.minimized_sessions:
                    raise RestoreError('PRESENTATION_CONFLICT',
                                       'Exact restore does not support minimized/maximized target panes.')
            elif row.get('attached') != 0 or clients:
                raise RestoreError('PRESENTATION_CONFLICT', 'zmx attached/client counts disagree.')
        return raw, observation

    async def _current_exact(self, raw, observation):
        if len(self.guids) != len(self.items):
            return False
        bindable = dict(observation, views={})
        try:
            content, _ = _bind_snapshot(workspace_for_layout(self.layout), raw, bindable)
        except LayoutError:
            return False
        current = {'schema': self.layout['schema'], 'capture': self.layout['capture'],
                   'windows': content['windows']}
        return structural_layout(current) == structural_layout(self.layout)

    async def preflight(self):
        blueprint = reconstruction_blueprint(self.layout)
        raw, observation = await self._discover()
        exact = await self._current_exact(raw, observation)
        existing_ttys = []
        for identity in self.initial_existing:
            session = self.app.get_session_by_id(self.guids[identity])
            if session is not None:
                existing_ttys.append(await session.async_get_variable('tty'))
        if not exact and self.caller_tty and self.caller_tty in existing_ttys:
            raise RestoreError('PRESENTATION_CONFLICT',
                               'The invoking pane is a target view that would need reparenting; run restore from another pane.')
        return {'blueprint': blueprint, 'already_exact': exact,
                'existing': len(self.initial_existing),
                'missing': len(self.items) - len(self.initial_existing)}

    async def _wait_created(self, item, window):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            await self._refresh()
            current = self.app.get_window_by_id(window.window_id)
            observation = self.runtime_reader()
            rows = _runtime_rows(self.items, observation)
            row = rows[item.identity]
            clients = set(observation['clients'].get(row['sid'], []))
            if row.get('attached', 0) > 1 or len(clients) > 1:
                raise RestoreError('PRESENTATION_PARTIAL',
                                   'A second client appeared while creating an exact view.')
            matches = []
            if current and len(clients) == 1:
                tty = next(iter(clients))
                for tab in list(current.tabs):
                    for session in list(tab.sessions):
                        if await session.async_get_variable('tty') == tty:
                            matches.append(session)
            if row.get('attached') == 1 and len(matches) == 1:
                session = matches[0]
                self.rows = rows
                await session.async_set_variable('user.cxdeck_name', item.display_name)
                await session.async_set_name(item.display_name)
                self.guids[item.identity] = session.session_id
                self.created.add(item.identity)
                return
            await asyncio.sleep(0.05)
        raise RestoreError('PRESENTATION_PARTIAL',
                           'Created iTerm view did not acquire the exact verified zmx client.')

    async def _create_missing(self):
        for item in self.items:
            if item.identity in self.guids:
                continue
            self._observe_rows()
            row = self.rows[item.identity]
            try:
                window = await self.iterm2.Window.async_create(
                    self.connection, profile=self.profile_name,
                    command=cx_iterm.attachment(row, self.context))
                if window is None:
                    raise RestoreError('PRESENTATION_PARTIAL', 'iTerm did not create an attach view.')
                await self._wait_created(item, window)
            except RestoreError:
                raise
            except Exception as exc:
                raise RestoreError('PRESENTATION_PARTIAL',
                                   'iTerm view creation failed; resolved runtimes remain alive. ' + str(exc)) from exc

    async def _own_window(self, identity):
        session, tab, window = await self._location(identity)
        if tab.minimized_sessions:
            raise RestoreError('PRESENTATION_CONFLICT', 'Cannot move a minimized/maximized target pane.')
        if len(tab.sessions) == 1 and len(window.tabs) == 1:
            return window
        before = window.window_id
        try:
            moved = await session.async_move_to_new_window()
        except Exception as exc:
            raise RestoreError('PRESENTATION_PARTIAL', 'Could not isolate a target window: ' + str(exc)) from exc
        self.moved.add(identity)
        _, _, window = await self._location(
            identity, expected_window=moved if isinstance(moved, str) else None,
            different_window=before)
        if window.window_id == before:
            raise RestoreError('PRESENTATION_PARTIAL',
                               f'iTerm did not confirm target-window isolation ({before} -> '
                               f'{window.window_id}; API={moved!r}).')
        return window

    async def _arrange_anchors(self, blueprint):
        targets = []
        for desired_window in blueprint['windows']:
            target_window = await self._own_window(desired_window['anchor'])
            tab_ids = []
            for index, desired_tab in enumerate(desired_window['tabs']):
                identity = desired_tab['anchor']
                session, source_tab, source_window = await self._location(identity)
                if index == 0:
                    tab_ids.append(source_tab.tab_id)
                    continue
                old_window, old_tab = source_window.window_id, source_tab.tab_id
                split_tab = len(source_tab.sessions) > 1
                try:
                    _, _, target_window = await self._location(desired_window['anchor'])
                    allowed = set(tab_ids)
                    if source_window.window_id == target_window.window_id:
                        allowed.add(source_tab.tab_id)
                    if any(tab.tab_id not in allowed for tab in list(target_window.tabs)):
                        raise RestoreError(
                            'PRESENTATION_CONFLICT',
                            'An unexpected tab appeared in a target window during restore.')
                    if split_tab:
                        await session.async_move_to_new_tab(target_window, tab_index=index)
                    elif source_window.window_id != target_window.window_id:
                        current = [tab for tab in target_window.tabs if tab.tab_id in tab_ids]
                        await target_window.async_set_tabs([*current, source_tab])
                    _, tab, _ = await self._location(
                        identity, expected_window=target_window.window_id,
                        different_tab=old_tab if split_tab else None)
                    tab_ids.append(tab.tab_id)
                    self.moved.add(identity)
                except Exception as exc:
                    raise RestoreError('PRESENTATION_PARTIAL',
                                       'Could not arrange exact tab membership: ' + str(exc)) from exc
                _, current_tab, current_window = await self._location(identity)
                if current_window.window_id == old_window and current_tab.tab_id == old_tab:
                    raise RestoreError('PRESENTATION_PARTIAL', 'iTerm did not move a required tab anchor.')
            _, _, target_window = await self._location(desired_window['anchor'])
            if {tab.tab_id for tab in list(target_window.tabs)} != set(tab_ids):
                raise RestoreError('PRESENTATION_CONFLICT',
                                   'Target tab membership changed before ordering.')
            ordered = [self.app.get_tab_by_id(tab_id) for tab_id in tab_ids]
            if any(tab is None for tab in ordered):
                raise RestoreError('PRESENTATION_PARTIAL', 'A target tab disappeared before ordering.')
            try:
                await target_window.async_set_tabs(ordered)
            except Exception as exc:
                raise RestoreError('PRESENTATION_PARTIAL', 'Could not order target tabs: ' + str(exc)) from exc
            targets.append((desired_window, target_window.window_id, tab_ids))
        return targets

    async def _apply_moves(self, blueprint):
        for move in blueprint['moves']:
            source = await self._session(tuple(move['source']))
            destination = await self._session(tuple(move['destination']))
            try:
                await self.app.async_move_session(
                    source, destination, split_vertically=move['axis'] == 'columns',
                    before=move['before'])
            except Exception as exc:
                raise RestoreError('PRESENTATION_PARTIAL', 'Could not reparent a verified Session: ' + str(exc)) from exc
            self.moved.add(tuple(move['source']))

    async def _present_names(self):
        for item in self.items:
            try:
                session = await self._session(item.identity)
                await session.async_set_variable('user.cxdeck_name', item.display_name)
                await session.async_set_name(item.display_name)
            except RestoreError:
                raise
            except Exception as exc:
                self.limitations.append('name: ' + str(exc))

    def _leaf_weights(self, root):
        result = {}
        def visit(node, columns, rows):
            if node['type'] == 'conversation':
                result[first_identity(node)] = (columns, rows)
                return
            ratios = node.get('ratio_hints')
            ratios = ratios if ratios and len(ratios) == len(node['children']) else None
            for index, child in enumerate(node['children']):
                fraction = ratios[index] if ratios else 1 / len(node['children'])
                visit(child, columns * fraction if node['axis'] == 'columns' else columns,
                      rows * fraction if node['axis'] == 'rows' else rows)
        visit(root, 1.0, 1.0)
        return result

    async def _apply_hints(self, targets):
        for desired_window, window_id, tab_ids in targets:
            frame = desired_window.get('frame_hint')
            if frame:
                try:
                    window = self.app.get_window_by_id(window_id)
                    await window.async_set_frame(self.iterm2.Frame(
                        self.iterm2.Point(frame['x'], frame['y']),
                        self.iterm2.Size(int(frame['width']), int(frame['height']))))
                except Exception as exc:
                    self.limitations.append('frame: ' + str(exc))
            for desired_tab, tab_id in zip(desired_window['tabs'], tab_ids):
                try:
                    tab = self.app.get_tab_by_id(tab_id)
                    weights = self._leaf_weights(desired_tab['root'])
                    spans = []
                    for identity in weights:
                        session = await self._session(identity)
                        spans.append((session.grid_size.width / max(weights[identity][0], 0.000001),
                                      session.grid_size.height / max(weights[identity][1], 0.000001)))
                    total_columns = max((value[0] for value in spans), default=80)
                    total_rows = max((value[1] for value in spans), default=24)
                    for identity, (columns, rows) in weights.items():
                        session = await self._session(identity)
                        session.preferred_size = self.iterm2.Size(
                            max(20, int(total_columns * columns)),
                            max(5, int(total_rows * rows)))
                    await tab.async_update_layout()
                except RestoreError:
                    raise
                except Exception as exc:
                    self.limitations.append('ratio: ' + str(exc))

    async def _verify_final(self):
        observation = self._observe_rows()
        raw = await self.reader.read()
        try:
            content, receipt = _bind_snapshot(
                workspace_for_layout(self.layout), raw, dict(observation, views={}))
        except LayoutError as exc:
            raise RestoreError('TOPOLOGY_MISMATCH', str(exc)) from exc
        actual = {'schema': self.layout['schema'], 'capture': self.layout['capture'],
                  'windows': content['windows']}
        if structural_layout(actual) != structural_layout(self.layout):
            raise RestoreError('TOPOLOGY_MISMATCH',
                               'Final iTerm window/tab/split tree differs from the saved exact layout.')
        by_identity = {}
        def visit(node):
            if node['type'] == 'session':
                by_identity[tuple(node['identity'])] = {'guid': node['guid'], 'tty': node['tty']}
                return
            for child in node['children']:
                visit(child)
        for window in receipt['windows']:
            for tab in window['tabs']:
                visit(tab['root'])
        return {self.rows[identity]['_key']: by_identity[identity] for identity in by_identity}

    async def restore(self):
        before = await self.preflight()
        if before['already_exact']:
            receipts = await self._verify_final()
            return RestoreResult('COMPLETE', len(self.initial_existing), 0, 0, 0, (), receipts)
        blueprint = before['blueprint']
        self._observe_rows()
        await self._create_missing()
        self._observe_rows()
        targets = await self._arrange_anchors(blueprint)
        await self._apply_moves(blueprint)
        await self._present_names()
        await self._apply_hints(targets)
        receipts = await self._verify_final()
        state = 'GEOMETRY_LIMITATION' if self.limitations else 'COMPLETE'
        moved_existing = self.moved & self.initial_existing
        return RestoreResult(state, len(self.initial_existing - moved_existing),
                             len(self.moved & self.initial_existing), 0,
                             len(self.created), tuple(self.limitations), receipts)


def _load_iterm2():
    try:
        import iterm2  # type: ignore
    except ImportError as exc:
        raise RestoreError(
            'PRESENTATION_CONFLICT',
            "Exact workspace restore requires the optional iTerm2 Python package. "
            "Install it explicitly with: python3 -m pip install --user 'iterm2==2.23'") from exc
    return iterm2


def preflight_live(layout, items, runtime_reader, context, caller_tty=''):
    """Read-only supported-API preflight; no pane/session mutation."""
    iterm2 = _load_iterm2()
    holder = {}
    async def main(connection):
        app = await iterm2.async_get_app(connection)
        if app is None:
            raise RestoreError('PRESENTATION_CONFLICT', 'iTerm2 hierarchy is unavailable.')
        restorer = PythonRestorer(iterm2, connection, app, layout, items,
                                  runtime_reader, context, caller_tty)
        holder['result'] = await restorer.preflight()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            iterm2.run_until_complete(main)
    except RestoreError:
        raise
    except Exception as exc:
        raise RestoreError('PRESENTATION_CONFLICT', 'iTerm2 exact-restore preflight failed: ' + str(exc)) from exc
    return holder['result']


def restore_live(layout, items, runtime_reader, context, caller_tty=''):
    """Apply MOVE_REUSE and return only after exact structural recapture."""
    iterm2 = _load_iterm2()
    holder = {}
    async def main(connection):
        app = await iterm2.async_get_app(connection)
        if app is None:
            raise RestoreError('PRESENTATION_CONFLICT', 'iTerm2 hierarchy is unavailable.')
        restorer = PythonRestorer(iterm2, connection, app, layout, items,
                                  runtime_reader, context, caller_tty)
        holder['result'] = await restorer.restore()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            iterm2.run_until_complete(main)
    except RestoreError:
        raise
    except Exception as exc:
        raise RestoreError('PRESENTATION_PARTIAL',
                           'iTerm2 exact reconstruction stopped; runtimes remain alive. ' + str(exc)) from exc
    return holder['result']
