#!/usr/bin/env python3
"""Disposable real-iTerm/zmx acceptance for exact workspace reconstruction.

The tool creates seven private zmx generations running a compiled inert process,
and mutates only the iTerm Sessions attached to those generations. It never
enumerates or changes the default zmx runtime and never starts Codex.

Run with:

    uv run --isolated --with 'iterm2==2.23' \
      python tools/iterm_restore_acceptance.py
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from importlib import metadata
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cx_iterm
import agent_console
import codex_resume
import console_entry
import cx_inventory
from cx_store import Store, live_key, thread_key
import cx_workspace_layout as capture
import cx_workspace_restore as restore
import cx_zmx
import workbench
from tools.iterm_move_reuse_probe import MoveProbe


def leaves(node):
    if node['type'] == 'conversation':
        yield tuple(node['identity'][key] for key in ('host', 'codex_home', 'thread_id'))
        return
    for child in node['children']:
        yield from leaves(child)


class Acceptance:
    def __init__(self, probe):
        self.probe = probe
        self.host = socket.gethostname()
        self.context = None
        self.workspace = None
        self.layout = None
        self.receipts = {}

    def runtime(self):
        data = cx_zmx.snapshot(bind_threads=False)
        context = data['context']
        processes = cx_zmx.processes()
        sessions, clients = [], {}
        for row in data['sessions']:
            if row['session'] not in self.probe.created_sessions:
                continue
            row = copy.deepcopy(row)
            row['_key'] = live_key(row, context)
            row['external_pids'] = []
            sessions.append(row)
            clients[row['sid']] = sorted(cx_zmx.client_ttys(row, processes))
        return {'context': context, 'sessions': sessions, 'clients': clients,
                'views': copy.deepcopy(self.receipts), 'unknown_pids': [],
                'process_failed': False}

    def inventory(self):
        return [dict(key=row['thread_id'], thread_id=row['thread_id'],
                     home=row['codex_home'], title=row['task'], cwd=self.probe.work,
                     managed=row, external_pids=[], state=row['state'])
                for row in self.runtime()['sessions']]

    async def arrange_fixture(self):
        app = self.probe.app
        # Window 1, tab 1: columns(A, rows(B, C)).
        await app.async_move_session(await self.probe.session('B'), await self.probe.session('A'),
                                     split_vertically=True, before=False)
        await app.async_move_session(await self.probe.session('C'), await self.probe.session('B'),
                                     split_vertically=False, before=True)
        # Window 1, tab 2: D.
        a, d = await self.probe.session('A'), await self.probe.session('D')
        await a.window.async_set_tabs([a.tab, d.tab])
        # Window 2: an N-ary columns(E, F, G) splitter.
        await app.async_move_session(await self.probe.session('F'), await self.probe.session('E'),
                                     split_vertically=True, before=False)
        await app.async_move_session(await self.probe.session('G'), await self.probe.session('F'),
                                     split_vertically=True, before=False)
        await self.probe.refresh()

    async def capture_layout(self):
        raw = await capture.ITermPythonReader(
            self.probe.iterm2, self.probe.app, metadata.version('iterm2')).read()
        runtime = self.runtime()
        locations = {}
        for window in raw['windows']:
            for tab in window['tabs']:
                stack = [tab['root']]
                while stack:
                    node = stack.pop()
                    if node['type'] == 'session':
                        locations[node['tty']] = node['guid']
                    else:
                        stack.extend(node['children'])
        members = []
        for row in runtime['sessions']:
            members.append({'home': row['codex_home'], 'thread_id': row['thread_id'],
                            'title': row['session'], 'cwd': self.probe.work,
                            'live_key': row['_key'], 'session': row['session']})
            tty = runtime['clients'][row['sid']][0]
            self.receipts[row['_key']] = {'guid': locations[tty], 'tty': tty}
        self.workspace = {'host': self.host, 'members': members, 'layout': {'per_tab': 0}}
        result = await capture.capture_contract(
            self.workspace,
            capture.ITermPythonReader(self.probe.iterm2, self.probe.app,
                                      metadata.version('iterm2')),
            self.runtime)
        self.layout = result.layout
        self.receipts = result.store_views
        return result.layout

    def plan(self):
        runtime = self.runtime()
        return restore.build_runtime_plan(
            self.layout, self.workspace, self.inventory(), [], False,
            host=self.host, codex_home=self.probe.codex_home,
            planned_cwd=lambda row, override: row['cwd'], context=runtime['context'])

    async def disturb(self):
        # Collapse D into A's split and move C beside E, changing both window,
        # tab, and nesting membership without replacing either iTerm Session.
        await self.probe.app.async_move_session(
            await self.probe.session('D'), await self.probe.session('A'),
            split_vertically=False, before=True)
        await self.probe.app.async_move_session(
            await self.probe.session('C'), await self.probe.session('E'),
            split_vertically=True, before=False)
        # G loses only its disposable presentation attach client; the private
        # zmx generation and dummy Codex process remain alive.
        await (await self.probe.session('G')).async_close(force=True)
        # iTerm can keep the attach client alive while an asynchronous close
        # drains presentation state. Give that supported close path enough time
        # to settle before treating the disposable acceptance fixture as stuck.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            g = next(row for row in self.runtime()['sessions'] if row['thread_id'] ==
                     self.probe.rows['G']['thread_id'])
            if g['attached'] == 0:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError('Disposable G attach client did not close.')

    async def restore_once(self):
        plan = self.plan()
        engine = restore.PythonRestorer(
            self.probe.iterm2, self.probe.connection, self.probe.app,
            plan.layout, plan.items, self.runtime, self.runtime()['context'], '')
        result = await engine.restore()
        self.receipts.update(result.view_receipts)
        runtime = self.runtime()
        by_thread = {row['thread_id']: row for row in runtime['sessions']}
        for name in self.probe.names:
            row = by_thread[self.probe.rows[name]['thread_id']]
            receipt = result.view_receipts[row['_key']]
            self.probe.ids[name]['guid'] = receipt['guid']
        return result

    async def interrupt_after_anchor_moves(self):
        class Interrupted(restore.PythonRestorer):
            async def _arrange_anchors(inner, blueprint):
                await super(Interrupted, inner)._arrange_anchors(blueprint)
                raise restore.RestoreError(
                    'PRESENTATION_PARTIAL', 'deliberate disposable interruption')
        plan = self.plan()
        engine = Interrupted(
            self.probe.iterm2, self.probe.connection, self.probe.app,
            plan.layout, plan.items, self.runtime, self.runtime()['context'], '')
        try:
            await engine.restore()
        except restore.RestoreError as exc:
            if 'deliberate disposable interruption' not in str(exc):
                raise
            return str(exc)
        raise RuntimeError('Disposable interruption did not interrupt restore.')

    async def close_views(self):
        await self.probe.refresh()
        owned_ttys = {tty for values in self.runtime()['clients'].values() for tty in values}
        sessions = []
        for window in list(self.probe.app.windows):
            for tab in list(window.tabs):
                sessions.extend(list(tab.sessions))
        for session in sessions:
            try:
                if await session.async_get_variable('tty') in owned_ttys:
                    await session.async_close(force=True)
            except Exception:
                pass


class NoProfileMutationITerm(cx_iterm.ITerm):
    """Keep this disposable acceptance from changing the user's dynamic profile."""
    def configure(self, timestamps=True):
        return None


async def run(connection, iterm2):
    app = await iterm2.async_get_app(connection)
    output = {'fixture': {}, 'interrupted_restore': {}, 'first_restore': {}, 'second_restore': {},
              't1_t2_t3': {}, 'runtime_preservation': {}, 'cleanup_errors': []}
    original_environment = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='cxdeck-t2-restore-') as temporary:
        probe = MoveProbe(iterm2, connection, app, temporary, names='ABCDEFG')
        acceptance = Acceptance(probe)
        try:
            probe.prepare()
            os.environ.clear()
            os.environ.update(probe.env)
            with contextlib.redirect_stdout(io.StringIO()):
                probe.create_runtimes()
            await probe.create_views()
            await acceptance.arrange_fixture()
            saved = await acceptance.capture_layout()
            before = await probe.facts()
            output['fixture'] = {
                'windows': len(saved['windows']),
                'tabs': sum(len(window['tabs']) for window in saved['windows']),
                'conversations': len(capture.conversation_identities(saved)),
                'nested': any(child.get('type') == 'split'
                              for window in saved['windows'] for tab in window['tabs']
                              for child in tab['root'].get('children', [])),
                'nary': any(tab['root'].get('type') == 'split' and
                            len(tab['root'].get('children', [])) >= 3
                            for window in saved['windows'] for tab in window['tabs']),
            }
            await acceptance.disturb()
            interrupted = await acceptance.interrupt_after_anchor_moves()
            g_after_interrupt = next(row for row in acceptance.runtime()['sessions']
                                     if row['thread_id'] == probe.rows['G']['thread_id'])
            output['interrupted_restore'] = {
                'state': interrupted.split(':', 1)[0],
                'missing_view_was_created': g_after_interrupt['attached'] == 1,
                'runtime_kept': g_after_interrupt['state'] == 'ALIVE',
            }
            first = await acceptance.restore_once()
            after_first = await probe.facts()
            recaptured = await capture.capture_contract(
                acceptance.workspace,
                capture.ITermPythonReader(iterm2, app, metadata.version('iterm2')),
                acceptance.runtime)
            first_exact = (restore.structural_layout(saved) ==
                           restore.structural_layout(recaptured.layout))
            second = await acceptance.restore_once()
            after_second = await probe.facts()
            output['first_restore'] = {
                'state': first.state,
                'existing_views_reused': first.existing_views_reused,
                'existing_views_moved': first.existing_views_moved,
                'existing_views_rebuilt': first.existing_views_rebuilt,
                'missing_views_created': first.missing_views_created,
                'final_recapture_exact': first_exact,
            }
            output['second_restore'] = {
                'state': second.state,
                'new_runtimes': 0,
                'new_clients': second.missing_views_created,
                'view_moves': second.existing_views_moved,
                'view_rebuilds': second.existing_views_rebuilt,
                'pid_changes': sum(before[name]['codex_pids'] != after_second[name]['codex_pids']
                                   for name in probe.names),
                'generation_changes': sum(before[name]['generation'] !=
                                          after_second[name]['generation']
                                          for name in probe.names),
            }
            output['runtime_preservation'] = {
                name: {'codex_pid_same': before[name]['codex_pids'] == after_first[name]['codex_pids'] ==
                                         after_second[name]['codex_pids'],
                       'generation_same': before[name]['generation'] == after_first[name]['generation'] ==
                                          after_second[name]['generation'],
                       'one_client_final': after_second[name]['attached'] == 1,
                       'existing_guid_same': (name == 'G' or before[name]['guid'] ==
                                              after_first[name]['guid'] == after_second[name]['guid']),
                       'existing_tty_same': (name == 'G' or before[name]['tty'] ==
                                             after_first[name]['tty'] == after_second[name]['tty'])}
                for name in probe.names}

            # Cross-feature gate: commit only synthetic organization/view data,
            # then drive T3 from the freshly MOVE_REUSE-restored T1 layout.
            store = Store(Path(temporary) / 'state')
            exact_workspace = copy.deepcopy(acceptance.workspace)
            exact_workspace['exact_layout'] = saved
            store.workspace('Exact Fixture', exact_workspace)
            overlap = copy.deepcopy(exact_workspace)
            overlap['members'] = overlap['members'][2:5]
            overlap.pop('exact_layout')
            store.workspace('Overlap', overlap)
            store.set_view_receipts(acceptance.receipts)
            group = store.create_group('Synthetic Studies')
            store.assign_group([
                thread_key(probe.codex_home, probe.rows[name]['thread_id'], socket.gethostname())
                for name in 'ABC'], group)
            store.annotate([thread_key(probe.codex_home, probe.rows['G']['thread_id'],
                                       socket.gethostname())], pinned=True)
            backend = console_entry.ResumeBackend(agent_console, store)
            history = [dict(key=probe.rows[name]['thread_id'],
                            thread_id=probe.rows[name]['thread_id'],
                            title='Synthetic ' + name, cwd=probe.work,
                            updated=time.time() - index, home=probe.codex_home,
                            managed=None, state='SAVED', external_pids=[])
                       for index, name in enumerate(probe.names)]
            gui = NoProfileMutationITerm()
            before_t3 = await probe.facts()
            with patch.object(codex_resume, 'list_history', return_value=(history, False)):
                inventory = workbench.inventory_snapshot(backend, gui=gui)
                if any(row['view_state'] != 'VERIFIED_VIEW'
                       for row in inventory['conversations']):
                    raise RuntimeError('T3 did not verify every MOVE_REUSE-restored view.')
                if len(cx_inventory.search(inventory['conversations'], 'synthetic studies')) != 3:
                    raise RuntimeError('T3 metadata search failed after exact restore.')
                caller = before_t3['A']['tty']
                with contextlib.redirect_stdout(io.StringIO()):
                    workbench.views_command(
                        ['status', '--workspace', 'Exact Fixture', '--json'], backend, gui=gui)
                    workbench.focus_navigation('next', backend, gui=gui, caller_tty=caller)
                    workbench.focus_navigation('previous', backend, gui=gui, caller_tty=caller)
                    workbench.views_command(
                        ['refresh', '--workspace', 'Exact Fixture'], backend, gui=gui)
                    workbench.views_command(
                        ['rebuild', '--workspace', 'Exact Fixture'], backend, gui=gui)
            after_t3 = await probe.facts()
            recaptured_after_t3 = await capture.capture_contract(
                acceptance.workspace,
                capture.ITermPythonReader(iterm2, app, metadata.version('iterm2')),
                acceptance.runtime)
            output['t1_t2_t3'] = {
                'inventory_schema': inventory['schema'],
                'verified_views': sum(row['view_state'] == 'VERIFIED_VIEW'
                                      for row in inventory['conversations']),
                'search_matches': len(cx_inventory.search(
                    inventory['conversations'], 'synthetic studies')),
                'topology_exact_after_scoped_rebuild': (
                    restore.structural_layout(saved) ==
                    restore.structural_layout(recaptured_after_t3.layout)),
                'new_clients': sum(after_t3[name]['attached'] - before_t3[name]['attached']
                                   for name in probe.names),
                'pid_changes': sum(after_t3[name]['codex_pids'] != before_t3[name]['codex_pids']
                                   for name in probe.names),
                'generation_changes': sum(after_t3[name]['generation'] !=
                                          before_t3[name]['generation']
                                          for name in probe.names),
                'guid_changes': sum(after_t3[name]['guid'] != before_t3[name]['guid']
                                    for name in probe.names),
                'tty_changes': sum(after_t3[name]['tty'] != before_t3[name]['tty']
                                   for name in probe.names),
                'overlap_members_unchanged': len(store.read()['workspaces']['Overlap']['members']) == 3,
            }
        finally:
            await acceptance.close_views()
            output['cleanup_errors'] = await probe.cleanup()
            os.environ.clear()
            os.environ.update(original_environment)
            shutil.rmtree(probe.runtime, ignore_errors=True)
    checks = [output['fixture'].get('windows') == 2,
              output['fixture'].get('tabs', 0) >= 3,
              output['fixture'].get('conversations', 0) >= 6,
              output['fixture'].get('nested'), output['fixture'].get('nary'),
              output['interrupted_restore'].get('state') == 'PRESENTATION_PARTIAL',
              output['interrupted_restore'].get('missing_view_was_created'),
              output['interrupted_restore'].get('runtime_kept'),
              output['first_restore'].get('final_recapture_exact'),
              output['first_restore'].get('missing_views_created') == 0,
              output['second_restore'].get('new_runtimes') == 0,
              output['second_restore'].get('new_clients') == 0,
              output['second_restore'].get('view_moves') == 0,
              output['second_restore'].get('view_rebuilds') == 0,
              output['second_restore'].get('pid_changes') == 0,
              output['second_restore'].get('generation_changes') == 0,
              output['t1_t2_t3'].get('inventory_schema') == cx_inventory.SCHEMA,
              output['t1_t2_t3'].get('verified_views') == len('ABCDEFG'),
              output['t1_t2_t3'].get('search_matches') == 3,
              output['t1_t2_t3'].get('topology_exact_after_scoped_rebuild'),
              output['t1_t2_t3'].get('new_clients') == 0,
              output['t1_t2_t3'].get('pid_changes') == 0,
              output['t1_t2_t3'].get('generation_changes') == 0,
              output['t1_t2_t3'].get('guid_changes') == 0,
              output['t1_t2_t3'].get('tty_changes') == 0,
              output['t1_t2_t3'].get('overlap_members_unchanged'),
              all(all(values.values()) for values in output['runtime_preservation'].values()),
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
