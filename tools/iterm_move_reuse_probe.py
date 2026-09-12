#!/usr/bin/env python3
"""Disposable iTerm/zmx proof for moving existing verified CX Deck views.

The probe creates a private zmx runtime, six dummy ``codex`` processes, and six
owned iTerm windows. It uses only supported iTerm2 Python API movement calls and
never reads terminal contents or sends terminal input.

Run with:

    uv run --isolated --with 'iterm2==2.23' \
      python tools/iterm_move_reuse_probe.py
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cx_iterm
import cx_zmx
from tools.iterm_topology_probe import RUN_VARIABLE, MARKER_VARIABLE, normalize_tree


STUB = r'''
#include <unistd.h>
int main(void) { for (;;) sleep(1); }
'''


def expected(axis, *children):
    return {'type': 'split', 'axis': axis, 'children': list(children)}


def marker(name):
    return {'type': 'session', 'marker': name}


class MoveProbe:
    def __init__(self, iterm2, connection, app, root, names='ABCDEF'):
        self.iterm2 = iterm2
        self.connection = connection
        self.app = app
        self.root = Path(root)
        self.run_id = 'cxdeck-t2-move-' + uuid.uuid4().hex
        self.binary = str(self.root / 'bin/zmx')
        self.runtime = '/tmp/cxt2-' + uuid.uuid4().hex[:12]
        self.codex_home = str((self.root / 'codex-home').resolve())
        self.work = str((self.root / 'work').resolve())
        self.created_sessions = []
        self.created_window_ids = []
        self.rows = {}
        self.ids = {}
        self.names = tuple(names)
        self.env = dict(os.environ, HOME=str(self.root), CODEX_HOME=self.codex_home,
                        ZMX_DIR=self.runtime, ZMX_DIR_MODE='0700', ZMX_LOG_MODE='0600',
                        PATH=str(self.root / 'bin') + os.pathsep + os.environ.get('PATH', ''))
        self.env.pop('ZMX_SESSION', None)
        self.env.pop('ZMX_SESSION_PREFIX', None)

    def prepare(self):
        for path in (self.root / 'bin', Path(self.codex_home), Path(self.work)):
            path.mkdir(parents=True, exist_ok=True)
        zmx = shutil.which('zmx')
        cc = shutil.which('cc')
        if not zmx or not cc:
            raise RuntimeError('The move probe requires local zmx and cc.')
        shutil.copy2(zmx, self.binary)
        source = self.root / 'codex.c'
        source.write_text(STUB)
        subprocess.run([cc, str(source), '-o', str(self.root / 'bin/codex')],
                       check=True, capture_output=True)

    def create_runtimes(self):
        for index, name in enumerate(self.names, 1):
            session = 'cx-agent-t2-move-' + name.lower() + '-' + self.run_id[-8:]
            labels = {'cx_managed': '1', 'cx_version': '0.8.0-dev',
                      'cx_zmx_version': '0.8.1',
                      'cx_thread_id': str(uuid.uuid5(uuid.NAMESPACE_URL, self.run_id + name)),
                      'cx_codex_home': cx_zmx.encode_path(self.codex_home),
                      'cx_launch_policy': 'safe', 'cx_launch_mode': 'resume',
                      'cx_launch_cwd': cx_zmx.encode_path(self.work)}
            child_env = dict(self.env, CX_AGENT_NAME=session, CX_MANAGED='1')
            row = cx_zmx.create(session, [str(self.root / 'bin/codex')], self.work,
                                labels, detached=True, binary=self.binary, env=child_env)
            self.created_sessions.append(session)
            self.rows[name] = row

    async def refresh(self):
        await self.app.async_refresh()
        await asyncio.sleep(0.08)

    async def create_views(self):
        context = {'backend': 'zmx', 'host': socket.gethostname(),
                   'runtime_dir': os.path.realpath(self.runtime), 'runtime_path': self.binary}
        for name in self.names:
            command = cx_iterm.attachment(self.rows[name], context)
            window = await self.iterm2.Window.async_create(self.connection, command=command)
            if window is None:
                raise RuntimeError('iTerm did not create a disposable attach view.')
            self.created_window_ids.append(window.window_id)
            await window.async_set_variable(RUN_VARIABLE, self.run_id)
            await self.refresh()
            window = self.app.get_window_by_id(window.window_id)
            session = window.tabs[0].sessions[0]
            await window.tabs[0].async_set_variable(RUN_VARIABLE, self.run_id)
            await session.async_set_variable(RUN_VARIABLE, self.run_id)
            await session.async_set_variable(MARKER_VARIABLE, name)
            self.ids[name] = {'guid': session.session_id, 'window_id': window.window_id,
                              'tab_id': window.tabs[0].tab_id}
        await self.wait_attached()

    async def wait_attached(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            snapshot = cx_zmx.snapshot(bind_threads=False)
            selected = {row['session']: row for row in snapshot['sessions']
                        if row['session'] in self.created_sessions}
            if len(selected) == len(self.names) and all(row['attached'] == 1 and row['codex_pids']
                                          for row in selected.values()):
                self.rows = {name: selected[self.rows[name]['session']] for name in self.names}
                return
            await asyncio.sleep(0.1)
        raise RuntimeError('Disposable zmx clients did not become verifiably attached.')

    def client_pid(self, row, tty):
        matches = [item['pid'] for item in cx_zmx.processes().values()
                   if Path(item['executable']).name == 'zmx' and
                   '/dev/' + item['tty'].removeprefix('/dev/') == tty]
        if len(matches) != 1:
            raise RuntimeError('Could not identify exactly one disposable zmx client PID.')
        return matches[0]

    async def facts(self):
        await self.refresh()
        result = {}
        for name in self.names:
            row = next(item for item in cx_zmx.snapshot(bind_threads=False)['sessions']
                       if item['session'] == self.rows[name]['session'])
            session = self.app.get_session_by_id(self.ids[name]['guid'])
            if session is None:
                raise RuntimeError('Moved disposable iTerm Session disappeared: ' + name)
            tty = await session.async_get_variable('tty')
            verified_ttys = cx_zmx.client_ttys(row)
            if verified_ttys != {tty}:
                raise RuntimeError('Generation-token verification failed after move: ' + name)
            result[name] = {
                'guid': session.session_id, 'tty': tty,
                'client_pid': self.client_pid(row, tty),
                'generation': list(cx_zmx.normalize_generation(row)),
                'codex_pids': list(row['codex_pids']), 'attached': row['attached']}
        return result

    async def session(self, name):
        await self.refresh()
        value = self.app.get_session_by_id(self.ids[name]['guid'])
        if value is None or await value.async_get_variable(RUN_VARIABLE) != self.run_id:
            raise RuntimeError('Refusing to move an unowned or missing session: ' + name)
        return value

    async def topology(self, names):
        first = await self.session(names[0])
        tab = first.tab
        async def node(value):
            if isinstance(value, self.iterm2.Session):
                return {'type': 'session',
                        'marker': await value.async_get_variable(MARKER_VARIABLE),
                        'session_id': value.session_id,
                        'tty': await value.async_get_variable('tty')}
            return {'type': 'split', 'axis': 'columns' if value.vertical else 'rows',
                    'children': [await node(child) for child in value.children]}
        return normalize_tree(await node(tab.root))

    async def run_movements(self):
        baseline = await self.facts()
        results = {}

        # B joins A as a split, then becomes a second tab in A's window.
        a, b = await self.session('A'), await self.session('B')
        await self.app.async_move_session(b, a, split_vertically=True, before=False)
        b = await self.session('B')
        a = await self.session('A')
        a_tab = a.tab
        b_tab_id = await b.async_move_to_new_tab(a.window, tab_index=0)
        await self.refresh()
        a, b = await self.session('A'), await self.session('B')
        await a.window.async_set_tabs([a.tab, b.tab])
        await self.refresh()
        results['tab_reorder_within_window'] = [tab.tab_id for tab in (await self.session('A')).window.tabs][:2] == [a_tab.tab_id, b_tab_id]

        # Move B's intact tab to D's window, then to its own new window.
        b, d = await self.session('B'), await self.session('D')
        b_tab_id = b.tab.tab_id
        await d.window.async_set_tabs([b.tab])
        await self.refresh()
        results['tab_move_between_windows'] = (await self.session('B')).window.window_id == (await self.session('D')).window.window_id
        b = await self.session('B')
        new_window = await b.tab.async_move_to_window()
        await self.refresh()
        new_window_id = getattr(new_window, 'window_id', new_window)
        results['tab_move_to_own_window'] = ((await self.session('B')).window.window_id == new_window_id and
                                             (await self.session('B')).tab.tab_id == b_tab_id)

        # C moves into A's tab, into a new tab in B's window, then beside D.
        a, c = await self.session('A'), await self.session('C')
        await self.app.async_move_session(c, a, split_vertically=False, before=False)
        c = await self.session('C')
        results['session_move_between_windows'] = c.window.window_id == (await self.session('A')).window.window_id
        await c.async_move_to_new_tab((await self.session('B')).window)
        await self.refresh()
        results['session_move_between_tabs'] = ((await self.session('C')).tab.tab_id !=
                                                (await self.session('B')).tab.tab_id)
        await self.app.async_move_session(await self.session('C'), await self.session('D'),
                                          split_vertically=True, before=False)
        await self.refresh()
        results['reparent_beside_session'] = ((await self.session('C')).tab.tab_id ==
                                              (await self.session('D')).tab.tab_id)

        # Construct two opposite mixed trees entirely by moving existing views.
        await self.app.async_move_session(await self.session('B'), await self.session('A'),
                                          split_vertically=True, before=False)
        await self.app.async_move_session(await self.session('C'), await self.session('B'),
                                          split_vertically=False, before=True)
        await self.app.async_move_session(await self.session('F'), await self.session('D'),
                                          split_vertically=False, before=True)
        await self.app.async_move_session(await self.session('E'), await self.session('D'),
                                          split_vertically=True, before=False)
        await self.refresh()
        left = await self.topology('ABC')
        right = await self.topology('DEF')
        results['arbitrary_nested_tree_one'] = left == expected(
            'columns', marker('A'), expected('rows', marker('B'), marker('C')))
        results['arbitrary_nested_tree_two'] = right == expected(
            'rows', expected('columns', marker('D'), marker('E')), marker('F'))

        final = await self.facts()
        preservation = {}
        for name in 'ABCDEF':
            preservation[name] = {
                'guid_same': final[name]['guid'] == baseline[name]['guid'],
                'tty_same': final[name]['tty'] == baseline[name]['tty'],
                'client_pid_same': final[name]['client_pid'] == baseline[name]['client_pid'],
                'generation_same': final[name]['generation'] == baseline[name]['generation'],
                'codex_pids_same': final[name]['codex_pids'] == baseline[name]['codex_pids'],
                'exactly_one_client': final[name]['attached'] == 1,
            }
        return {'operations': results, 'topologies': {'ABC': left, 'DEF': right},
                'preservation': preservation,
                'policy': 'MOVE_REUSE' if all(results.values()) and all(
                    all(values.values()) for values in preservation.values()) else 'NOT_PROVEN'}

    async def cleanup(self):
        errors = []
        await self.refresh()
        for window in list(self.app.windows):
            try:
                if await window.async_get_variable(RUN_VARIABLE) == self.run_id:
                    await window.async_close(force=True)
            except Exception as exc:
                errors.append('iTerm cleanup: ' + str(exc))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if all(cx_zmx.exact_session(name)['attached'] == 0
                       for name in self.created_sessions):
                    break
            except Exception:
                break
            await asyncio.sleep(0.1)
        if self.created_sessions:
            result = subprocess.run([self.binary, 'kill', *self.created_sessions, '--force'],
                                    env=self.env, text=True, capture_output=True, timeout=8)
            if result.returncode:
                errors.append('zmx cleanup: ' + result.stderr.strip())
        return errors


async def run(connection, iterm2):
    app = await iterm2.async_get_app(connection)
    with tempfile.TemporaryDirectory(prefix='cxdeck-t2-move-') as temporary:
        probe = MoveProbe(iterm2, connection, app, temporary)
        output = {'api': {
            'tab_reorder': 'Window.async_set_tabs',
            'tab_between_windows': 'Window.async_set_tabs',
            'tab_to_own_window': 'Tab.async_move_to_window',
            'session_to_tab': 'Session.async_move_to_new_tab',
            'session_to_window': 'Session.async_move_to_new_window',
            'session_reparent': 'App.async_move_session'}}
        original_environment = dict(os.environ)
        try:
            probe.prepare()
            # All cx_zmx observations are thereby pinned to the private runtime;
            # no default/user zmx state is inventoried by this probe.
            os.environ.clear()
            os.environ.update(probe.env)
            with contextlib.redirect_stdout(io.StringIO()):
                probe.create_runtimes()
            await probe.create_views()
            output.update(await probe.run_movements())
        finally:
            output['cleanup_errors'] = await probe.cleanup()
            os.environ.clear()
            os.environ.update(original_environment)
            shutil.rmtree(probe.runtime, ignore_errors=True)
        output['result'] = ('PASS' if output.get('policy') == 'MOVE_REUSE' and
                            not output['cleanup_errors'] else 'FAIL')
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
