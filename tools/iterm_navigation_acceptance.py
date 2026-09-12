#!/usr/bin/env python3
"""Disposable real-iTerm/zmx acceptance for T3 navigation and view health.

Creates six private zmx generations running an inert binary.  It inventories
only that private runtime and mutates only iTerm windows tagged by the probe.

Run with:

    uv run --isolated --with 'iterm2==2.23' \
      python tools/iterm_navigation_acceptance.py
"""
from __future__ import annotations

import asyncio
import contextlib
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

import agent_console
import codex_resume
import console_entry
import cx_inventory
import cx_iterm
from cx_store import Store, live_key, thread_key
import cx_zmx
import workbench
from tools.iterm_move_reuse_probe import MoveProbe
from tools.iterm_topology_probe import RUN_VARIABLE


class NoProfileMutationITerm(cx_iterm.ITerm):
    def configure(self, timestamps=True):
        return None


async def wait_for(predicate, message, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.08)
    raise RuntimeError(message)


def runtime_facts(probe):
    rows = {row['session']: row for row in cx_zmx.snapshot(bind_threads=False)['sessions']
            if row['session'] in probe.created_sessions}
    return {name: {'generation': list(cx_zmx.normalize_generation(rows[probe.rows[name]['session']])),
                   'codex_pids': list(rows[probe.rows[name]['session']]['codex_pids']),
                   'attached': rows[probe.rows[name]['session']]['attached']}
            for name in probe.names}


def synthetic_history(probe):
    now = time.time()
    return [dict(key=probe.rows[name]['thread_id'], thread_id=probe.rows[name]['thread_id'],
                 title='Synthetic ' + name, cwd=probe.work, updated=now - index,
                 home=probe.codex_home, managed=None, state='SAVED', external_pids=[])
            for index, name in enumerate(probe.names)]


async def run(connection, iterm2):
    app = await iterm2.async_get_app(connection)
    original_environment = dict(os.environ)
    output = {'classifications': {}, 'metadata': {}, 'navigation': {}, 'workspace_refresh': {},
              'workspace_rebuild': {},
              'runtime_preservation': {}, 'cleanup_errors': []}
    with tempfile.TemporaryDirectory(prefix='cxdeck-t3-navigation-') as temporary:
        probe = MoveProbe(iterm2, connection, app, temporary, names='ABCDEF')
        gui = NoProfileMutationITerm()
        try:
            probe.prepare()
            os.environ.clear()
            os.environ.update(probe.env)
            with contextlib.redirect_stdout(io.StringIO()):
                probe.create_runtimes()
            await probe.create_views()
            await probe.refresh()
            store = Store(Path(temporary) / 'state')
            backend = console_entry.ResumeBackend(agent_console, store)
            history = synthetic_history(probe)
            facts = await probe.facts()
            rows = backend.raw_snapshot(bind_threads=False)['sessions']
            selected = {row['thread_id']: row for row in rows
                        if row['session'] in probe.created_sessions}
            for name in probe.names:
                row = selected[probe.rows[name]['thread_id']]
                store.set_view_receipts({live_key(row, rows_context(backend)): {
                    'guid': facts[name]['guid'], 'tty': facts[name]['tty']}})
            g1 = store.create_group('Model Studies')
            g2 = store.create_group('Archive Demos')
            store.assign_group([thread_key(probe.codex_home, probe.rows[name]['thread_id'],
                                           socket.gethostname()) for name in 'ABC'], g1)
            store.assign_group([thread_key(probe.codex_home, probe.rows[name]['thread_id'],
                                           socket.gethostname()) for name in 'DEF'], g2)
            pinned_key = thread_key(probe.codex_home, probe.rows['F']['thread_id'],
                                    socket.gethostname())
            store.annotate([pinned_key], pinned=True)
            members = [dict(home=probe.codex_home, thread_id=probe.rows[name]['thread_id'],
                            live_key=live_key(selected[probe.rows[name]['thread_id']], rows_context(backend)),
                            title='Synthetic ' + name, cwd=probe.work)
                       for name in probe.names]
            store.workspace('Daily', {'host': socket.gethostname(), 'members': members,
                                      'saved_at': time.time(), 'layout': {'per_tab': 0}})
            store.workspace('Subset', {'host': socket.gethostname(), 'members': members[:2],
                                       'saved_at': time.time(), 'layout': {'per_tab': 0}})
            # Make A's receipt stale and remove only F's presentation client.
            akey = members[0]['live_key']
            store.change(lambda state: state['views'].__setitem__(
                akey, {'guid': 'stale-guid', 'tty': '/dev/ttys999'}))
            await (await probe.session('F')).async_close(force=True)
            await wait_for(lambda: runtime_facts(probe)['F']['attached'] == 0,
                           'Disposable no-view client did not close.')
            before_read = runtime_facts(probe)

            with patch.object(codex_resume, 'list_history', return_value=(history, False)):
                first = workbench.inventory_snapshot(backend, gui=gui)
                states = {record['display']['name']: record['view_state']
                          for record in first['conversations']}
                output['classifications'] = states
                if sorted(states.values()).count('STALE_RECEIPT') != 1 or \
                        sorted(states.values()).count('NO_VIEW') != 1 or \
                        sorted(states.values()).count('VERIFIED_VIEW') != 4:
                    raise RuntimeError('Disposable view-health classifications were wrong: ' + repr(states))
                if len(cx_inventory.search(first['conversations'], 'model studies')) != 3:
                    raise RuntimeError('Group metadata search did not find the expected three records.')
                if len(cx_inventory.search(first['conversations'], probe.work)) != 6:
                    raise RuntimeError('cwd metadata search did not find all six records.')
                if first['conversations'][0]['display']['name'] != 'Synthetic F' or not \
                        first['conversations'][0]['display']['pinned']:
                    raise RuntimeError('Pinned ordering did not put Synthetic F first.')
                output['metadata'] = {
                    'groups': sorted({record['display']['group']
                                      for record in first['conversations']}),
                    'pinned_first': first['conversations'][0]['display']['name'],
                    'search_group_matches': len(cx_inventory.search(
                        first['conversations'], 'model studies')),
                    'search_cwd_matches': len(cx_inventory.search(first['conversations'], probe.work)),
                    'workspaces': sorted(store.read()['workspaces']),
                }
                with contextlib.redirect_stdout(io.StringIO()):
                    workbench.views_command(['status', '--workspace', 'Daily', '--json'],
                                            backend, gui=gui)
                    workbench.find_command(['synthetic'], backend)
                    workbench.focus_navigation('next', backend, gui=gui,
                                               caller_tty=facts['A']['tty'])
                    workbench.focus_navigation('previous', backend, gui=gui,
                                               caller_tty=facts['A']['tty'])
                after_read = runtime_facts(probe)
                output['navigation'] = {
                    'new_clients': sum(after_read[name]['attached'] - before_read[name]['attached']
                                       for name in probe.names),
                    'pid_changes': sum(after_read[name]['codex_pids'] != before_read[name]['codex_pids']
                                       for name in probe.names),
                    'generation_changes': sum(after_read[name]['generation'] != before_read[name]['generation']
                                              for name in probe.names),
                }

                before_refresh = runtime_facts(probe)
                with contextlib.redirect_stdout(io.StringIO()):
                    workbench.views_command(['refresh', '--workspace', 'Daily'], backend, gui=gui)
                after_refresh = runtime_facts(probe)
                refreshed = workbench.inventory_snapshot(backend, history=history, gui=gui)
                a_record = next(record for record in refreshed['conversations']
                                if record['identity']['thread_id'] == probe.rows['A']['thread_id'])
                output['workspace_refresh'] = {
                    'stale_receipt_repaired': a_record['view_state'] == 'VERIFIED_VIEW',
                    'new_clients': sum(after_refresh[name]['attached'] - before_refresh[name]['attached']
                                       for name in probe.names),
                    'pid_changes': sum(after_refresh[name]['codex_pids'] != before_refresh[name]['codex_pids']
                                       for name in probe.names),
                    'generation_changes': sum(after_refresh[name]['generation'] !=
                                              before_refresh[name]['generation']
                                              for name in probe.names),
                    'other_workspace_unchanged': len(store.read()['workspaces']['Subset']['members']) == 2,
                }

                # A temporary second client must be reported and must block rebuild.
                brow = selected[probe.rows['B']['thread_id']]
                command = cx_iterm.attachment(brow, rows_context(backend))
                duplicate_window = await iterm2.Window.async_create(connection, command=command)
                await duplicate_window.async_set_variable(RUN_VARIABLE, probe.run_id)
                await wait_for(lambda: runtime_facts(probe)['B']['attached'] == 2,
                               'Disposable second client did not attach.')
                multiple = workbench.inventory_snapshot(backend, history=history, gui=gui)
                b_record = next(record for record in multiple['conversations']
                                if record['identity']['thread_id'] == probe.rows['B']['thread_id'])
                if b_record['view_state'] != 'MULTIPLE_CLIENTS':
                    raise RuntimeError('Disposable second client was not classified as MULTIPLE_CLIENTS.')
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        workbench.views_command(['rebuild', '--workspace', 'Daily'], backend, gui=gui)
                except RuntimeError as exc:
                    if 'Ambiguous presentation' not in str(exc):
                        raise
                else:
                    raise RuntimeError('Workspace rebuild did not block a multiple-client ambiguity.')
                await duplicate_window.async_close(force=True)
                await wait_for(lambda: runtime_facts(probe)['B']['attached'] == 1,
                               'Disposable second client did not detach after its window closed.')

                before_rebuild = runtime_facts(probe)
                with contextlib.redirect_stdout(io.StringIO()):
                    workbench.views_command(['rebuild', '--workspace', 'Daily'], backend, gui=gui)
                await wait_for(lambda: runtime_facts(probe)['F']['attached'] == 1,
                               'Workspace rebuild did not create F presentation.')
                after_rebuild = runtime_facts(probe)
                final = workbench.inventory_snapshot(backend, history=history, gui=gui)
                if any(record['view_state'] != 'VERIFIED_VIEW' for record in final['conversations']):
                    raise RuntimeError('Workspace rebuild did not leave six verified views.')
                before_second = runtime_facts(probe)
                with contextlib.redirect_stdout(io.StringIO()):
                    workbench.views_command(['rebuild', '--workspace', 'Daily'], backend, gui=gui)
                after_second = runtime_facts(probe)
                output['workspace_rebuild'] = {
                    'first_new_clients': sum(after_rebuild[name]['attached'] - before_rebuild[name]['attached']
                                             for name in probe.names),
                    'second_new_clients': sum(after_second[name]['attached'] - before_second[name]['attached']
                                              for name in probe.names),
                    'all_verified': True,
                    'other_workspace_unchanged': len(store.read()['workspaces']['Subset']['members']) == 2,
                }
                output['runtime_preservation'] = {
                    'pid_changes': sum(before_read[name]['codex_pids'] != after_second[name]['codex_pids']
                                       for name in probe.names),
                    'generation_changes': sum(before_read[name]['generation'] != after_second[name]['generation']
                                              for name in probe.names),
                }
        finally:
            output['cleanup_errors'] = await probe.cleanup()
            os.environ.clear()
            os.environ.update(original_environment)
            shutil.rmtree(probe.runtime, ignore_errors=True)
    checks = [output['navigation'].get('new_clients') == 0,
              output['navigation'].get('pid_changes') == 0,
              output['navigation'].get('generation_changes') == 0,
              output['workspace_refresh'].get('stale_receipt_repaired'),
              output['workspace_refresh'].get('new_clients') == 0,
              output['workspace_refresh'].get('pid_changes') == 0,
              output['workspace_refresh'].get('generation_changes') == 0,
              output['workspace_refresh'].get('other_workspace_unchanged'),
              output['workspace_rebuild'].get('first_new_clients') == 1,
              output['workspace_rebuild'].get('second_new_clients') == 0,
              output['workspace_rebuild'].get('all_verified'),
              output['workspace_rebuild'].get('other_workspace_unchanged'),
              output['runtime_preservation'].get('pid_changes') == 0,
              output['runtime_preservation'].get('generation_changes') == 0,
              output['metadata'].get('pinned_first') == 'Synthetic F',
              output['metadata'].get('search_group_matches') == 3,
              output['metadata'].get('search_cwd_matches') == 6,
              not output['cleanup_errors']]
    output['result'] = 'PASS' if all(checks) else 'FAIL'
    return output


def rows_context(backend):
    return backend.raw_snapshot(bind_threads=False)['context']


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
        holder['result']['incidental_stdout'] = incidental.getvalue().strip()
    print(json.dumps(holder['result'], indent=2, sort_keys=True))
    return 0 if holder['result']['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
