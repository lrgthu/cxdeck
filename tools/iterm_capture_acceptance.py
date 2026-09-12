#!/usr/bin/env python3
"""Disposable real-iTerm acceptance for the production T1 capture contract.

The fixture contains only ``/bin/sleep`` processes. Runtime bindings are
synthetic exact identities and never query or mutate zmx or Codex.

Run with:

    uv run --isolated --with 'iterm2==2.23' \
      python tools/iterm_capture_acceptance.py
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from importlib import metadata
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cx_workspace_layout as layout
from tools.iterm_topology_probe import FIXTURES, LiveProbe


def leaves(node):
    if node['type'] == 'session':
        yield node
        return
    for child in node['children']:
        yield from leaves(child)


def fixture_leaves(snapshot):
    return [leaf for window in snapshot['windows'] for tab in window['tabs']
            for leaf in leaves(tab['root'])]


def structural(value):
    def node(item):
        if item['type'] == 'conversation':
            return {'type': 'conversation', 'identity': item['identity']}
        return {'type': 'split', 'axis': item['axis'],
                'children': [node(child) for child in item['children']]}
    return {'windows': [{'tabs': [{'root': node(tab['root'])} for tab in window['tabs']]}
                        for window in value['windows']]}


def without_timestamp(value):
    copied = copy.deepcopy(value)
    copied['capture'].pop('captured_at', None)
    return copied


def synthetic_binding(snapshot, root, run_id):
    records = sorted(fixture_leaves(snapshot), key=lambda item: item['marker'])
    host = socket.gethostname()
    home = str((root / 'codex-home').resolve())
    runtime_dir = str((root / 'zmx-runtime').resolve())
    members, sessions, clients, views = [], [], {}, {}
    for index, item in enumerate(records, 1):
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, run_id + ':' + item['marker']))
        session = 'cxdeck-t1-' + item['marker'].lower()
        sid = 'synthetic-' + item['marker'].lower()
        generation = {'host': host, 'runtime_dir': runtime_dir, 'session': session,
                      'daemon_pid': 90000 + index, 'created': 200000 + index}
        key = 'synthetic-live-' + item['marker'].lower()
        members.append({'home': home, 'thread_id': thread_id, 'title': item['marker'],
                        'live_key': key, 'session': session})
        sessions.append({'backend': 'zmx', 'session': session, 'sid': sid,
                         'daemon_pid': generation['daemon_pid'], 'created': generation['created'],
                         'generation': generation, 'codex_home': home,
                         'thread_id': thread_id, 'state': 'ALIVE', 'attached': 1,
                         'task': item['marker'], '_key': key})
        clients[sid] = [item['tty']]
        views[key] = {'guid': item['session_id'], 'tty': item['tty']}
    workspace = {'host': host, 'members': members, 'layout': {'per_tab': 0}}
    runtime = {'context': {'backend': 'zmx', 'host': host, 'runtime_dir': runtime_dir},
               'sessions': sessions, 'clients': clients, 'views': views}
    return workspace, runtime


class MutationReader:
    def __init__(self, reader, mutate):
        self.reader = reader
        self.mutate = mutate
        self.calls = 0

    async def read(self):
        self.calls += 1
        if self.calls == 2:
            await self.mutate()
        return await self.reader.read()


async def run(connection, iterm2):
    app = await iterm2.async_get_app(connection)
    run_id = 'cxdeck-t1-' + uuid.uuid4().hex
    probe = LiveProbe(iterm2, connection, app, run_id)
    provider = layout.ITermPythonReader(iterm2, app, metadata.version('iterm2'))
    output = {'run_id': run_id, 'fixture': {}, 'resize': {}, 'races': {}, 'cleanup_errors': []}
    with tempfile.TemporaryDirectory(prefix='cxdeck-t1-') as temporary:
        root = Path(temporary).resolve()
        try:
            # One owned window, two tabs, six harmless sessions. Its second tab
            # is the T0-proven three-child N-ary splitter.
            fixture = await probe.create_fixture('T1-production', [FIXTURES['T0-E'][0]])
            observed = await probe.capture_consistent(fixture)
            workspace, runtime = synthetic_binding(observed, root, run_id)
            runtime_reader = lambda: copy.deepcopy(runtime)
            first = await layout.capture_contract(workspace, provider, runtime_reader)
            second = await layout.capture_contract(workspace, provider, runtime_reader)
            layout.validate_layout(first.layout)
            output['fixture'] = {
                'windows': len(first.layout['windows']),
                'tabs': sum(len(window['tabs']) for window in first.layout['windows']),
                'sessions': len(layout.conversation_identities(first.layout)),
                'contains_nary_splitter': any(
                    tab['root'].get('type') == 'split' and len(tab['root'].get('children', [])) >= 3
                    for window in first.layout['windows'] for tab in window['tabs']),
                'repeat_durable_byte_identical': without_timestamp(first.layout) == without_timestamp(second.layout),
                'schema_valid': True,
            }

            window = await probe._owned_window(fixture.window_ids[0])
            frame = await window.async_get_frame()
            await window.async_set_frame(iterm2.Frame(
                frame.origin, iterm2.Size(int(frame.size.width) + 173, int(frame.size.height) + 79)))
            await asyncio.sleep(0.2)
            resized = await layout.capture_contract(workspace, provider, runtime_reader)
            output['resize'] = {
                'structural_tree_unchanged': structural(first.layout) == structural(resized.layout),
                'capture_still_valid': layout.validate_layout(resized.layout) == resized.layout,
            }
            await probe.close_fixture(fixture)

            create_fixture = await probe.create_fixture(
                'T1-race-create', [[FIXTURES['T0-A'][0][0]]])
            create_snapshot = await probe.capture_consistent(create_fixture)
            create_workspace, create_runtime = synthetic_binding(create_snapshot, root, run_id + '-create')
            anchor_id = fixture_leaves(create_snapshot)[0]['session_id']
            async def create_pane():
                anchor = await probe._owned_session(anchor_id)
                child = await anchor.async_split_pane(
                    vertical=False, before=False, profile_customizations=probe._sleep_profile())
                await probe._mark_leaf(child, 'CREATED-DURING-CAPTURE')
                await asyncio.sleep(0.1)
            try:
                await layout.capture_contract(
                    create_workspace, MutationReader(provider, create_pane),
                    lambda: copy.deepcopy(create_runtime), attempts=1)
                output['races']['pane_created'] = 'MISSED'
            except layout.LayoutChanged as exc:
                output['races']['pane_created'] = 'FAIL_CLOSED: ' + str(exc)
            await probe.close_fixture(create_fixture)

            close_fixture = await probe.create_fixture(
                'T1-race-close', [[FIXTURES['T0-A'][0][0]]])
            close_snapshot = await probe.capture_consistent(close_fixture)
            close_workspace, close_runtime = synthetic_binding(close_snapshot, root, run_id + '-close')
            closing_id = fixture_leaves(close_snapshot)[-1]['session_id']
            async def close_pane():
                session = await probe._owned_session(closing_id)
                await session.async_close(force=True)
                await asyncio.sleep(0.1)
            try:
                await layout.capture_contract(
                    close_workspace, MutationReader(provider, close_pane),
                    lambda: copy.deepcopy(close_runtime), attempts=1)
                output['races']['pane_closed'] = 'MISSED'
            except layout.LayoutChanged as exc:
                output['races']['pane_closed'] = 'FAIL_CLOSED: ' + str(exc)
            await probe.close_fixture(close_fixture)
        finally:
            output['cleanup_errors'] = await probe.cleanup_all()
    checks = [output['fixture'].get('windows') == 1,
              output['fixture'].get('tabs') == 2,
              output['fixture'].get('sessions', 0) >= 5,
              output['fixture'].get('contains_nary_splitter'),
              output['fixture'].get('repeat_durable_byte_identical'),
              output['resize'].get('structural_tree_unchanged'),
              output['resize'].get('capture_still_valid'),
              all(str(value).startswith('FAIL_CLOSED') for value in output['races'].values()),
              len(output['races']) == 2,
              not output['cleanup_errors']]
    output['result'] = 'PASS' if all(checks) else 'FAIL'
    return output


def main():
    try:
        import iterm2  # type: ignore
    except ImportError:
        print(json.dumps({'error': "Run with uv --with 'iterm2==2.23'."}, indent=2))
        return 2
    holder = {}
    async def connected(connection):
        holder['result'] = await run(connection, iterm2)
    incidental = io.StringIO()
    try:
        with contextlib.redirect_stdout(incidental):
            iterm2.run_until_complete(connected)
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}, indent=2))
        return 1
    if incidental.getvalue().strip():
        holder['result']['iterm2_incidental_stdout'] = incidental.getvalue().strip()
    print(json.dumps(holder['result'], indent=2, sort_keys=True))
    return 0 if holder['result']['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
