"""Workbench over zmx conversations and native iTerm views.

No prompts, approvals, automatic commits, process kills or remote commands.
"""
from __future__ import annotations

import argparse
import copy
import curses
import json
import math
import os
import socket
import sys
import time

from cx_store import Store, StateError, live_key, thread_key, name
import cx_iterm
from cx_version import VERSION


def native():
    import codex_resume
    return codex_resume


def enrich(data, b, store):
    ctx = data.get('context')
    if not isinstance(ctx, dict) or ctx.get('backend') != 'zmx' or not ctx.get('runtime_dir'):
        raise StateError('Cannot identify the zmx runtime; no live generation guessed.')
    ctx = dict(ctx)
    saved = store.read()['agents']
    for row in data['sessions']:
        row['_key'] = live_key(row, ctx)
        row['_thread_key'] = thread_key(row.get('codex_home'), row.get('thread_id'), ctx['host'])
        meta = dict(saved.get(row['_key'], {}))
        meta.update(saved.get(row['_thread_key'], {}))
        row['custom_name'] = meta.get('name')
        row['display_name'] = meta.get('name') or row['task']
        row['pinned'] = bool(meta.get('pinned'))
        row['root'] = meta.get('root', row.get('root', ''))
        row['launch_cwd'] = meta.get('launch_cwd', row.get('launch_cwd', ''))
    data['context'] = ctx
    return data


def resolve(token, b):
    rows = b.snapshot()['sessions']
    exact = [r for r in rows if r['session'] == token]
    matches = exact or [r for r in rows if token in (r['task'], r.get('display_name'), r.get('thread_id'))]
    if len(matches) != 1:
        raise StateError('Session name/label must match exactly and unambiguously. Use cxl for full names.')
    return matches[0]


def catalog(b, history=()):
    r = native()
    rows, unknown, warnings, failed = r.inventory(history, r.codex_home(), b)
    state = b.store.read()
    groups = state.get('groups', {})
    for item in rows:
        s = item.get('managed')
        if s:
            item['title'] = s.get('custom_name') or item['title']
            item['pinned'] = s.get('pinned', False)
        key = thread_key(item.get('home'), item.get('thread_id'), socket.gethostname())
        metadata = state['agents'].get(key, {}) if key else {}
        group_id = metadata.get('group_id')
        group = groups.get(group_id)
        item['group_id'] = group_id if group else None
        item['group'] = group.get('name') if group else 'Ungrouped'
        item['group_order'] = group.get('order', 0) if group else 10**9
        item['pinned'] = bool(metadata.get('pinned', item.get('pinned', False)))
        item['title'] = metadata.get('name', item['title'])
    rows.sort(key=lambda v: (v['group_order'], v['group'].casefold(),
                             not v.get('pinned', False), not bool(v.get('managed')), -v['updated']))
    return rows, unknown, warnings, failed


def annotate(token, b, title=None, pinned=None):
    row = resolve(token, b)
    observed = next((x for x in catalog(b)[0] if (x.get('managed') or {}).get('_key') == row['_key']), None)
    keys = [row['_key'], row.get('_thread_key')]
    if observed and observed.get('thread_id'):
        keys.append(thread_key(observed['home'], observed['thread_id'], socket.gethostname()))
    values = {}
    if title is not None:
        values['name'] = name(title)
    if pinned is not None:
        values['pinned'] = pinned
    b.store.annotate(keys, **values)
    if title is not None and row.get('attached', 0):
        updated = dict(row, display_name=values['name'], custom_name=values['name'])
        try:
            result = cx_iterm.refresh([updated], b, b.store)
        except (RuntimeError, OSError) as exc:
            raise StateError('Display name was saved, but iTerm2 presentation refresh failed. '
                             'The Codex process and zmx generation are unchanged. ' + str(exc)) from exc
        detail = (f" Presentation refreshed in {result['refreshed']} verified view(s)."
                  if result['refreshed'] else ' No verified current iTerm2 view needed an update.')
    elif title is not None:
        detail = ' No attached view needed an update.'
    else:
        detail = ''
    print('Updated local display metadata. Conversation ID, zmx generation and process unchanged.' + detail)


def layout(args):
    if args.per_tab < 0 or args.min_columns < 20 or args.min_rows < 5:
        raise StateError('Layout needs --per-tab >= 0, --min-columns >= 20, --min-rows >= 5.')
    return dict(per_tab=args.per_tab, min_columns=args.min_columns, min_rows=args.min_rows)


def focus_rows(rows, b, mode='window', **options):
    result = cx_iterm.show(rows, b, b.store, mode=mode, **options)
    print(f"Views: {result['opened']} opened, {result['reused']} reused; no agent restarted.")
    return result


def views_command(argv, b):
    parser = argparse.ArgumentParser(prog='cx views')
    parser.add_argument('command', choices=('rebuild', 'refresh'))
    args = parser.parse_args(argv)
    try:
        history, _ = native().list_history(native().codex_home(), 2000)
    except (RuntimeError, OSError):
        history = []
    rows = []
    for item in catalog(b, history)[0]:
        row = item.get('managed')
        if row and row.get('state') in ('ALIVE', 'STOPPED'):
            rows.append(dict(row, display_name=item['title']))
    if args.command == 'refresh':
        result = cx_iterm.refresh(rows, b, b.store) if rows else dict(refreshed=0, missing=0)
        print(f"Presentation refreshed: {result['refreshed']}; no verified view: {result['missing']}. No agent restarted.")
        return 0
    result = focus_rows(rows, b, mode='window') if rows else dict(opened=0, reused=0)
    print(f"Verified zmx targets: {len(rows)}. Views opened: {result['opened']}; reused: {result['reused']}.")
    return 0


def new_agents(argv, b):
    p = argparse.ArgumentParser(prog='cx new')
    place = p.add_mutually_exclusive_group()
    place.add_argument('--split', action='store_true')
    place.add_argument('--tab', action='store_true')
    place.add_argument('--window', action='store_true')
    p.add_argument('--count', type=int, default=1)
    p.add_argument('label', nargs='?')
    p.add_argument('codex_args', nargs=argparse.REMAINDER)
    boundary = argv.index('--') if '--' in argv else len(argv)
    a = p.parse_args(argv[:boundary])
    native_tail = argv[boundary + 1:] if boundary < len(argv) else None
    if a.count < 1 or (a.label and a.count != 1):
        raise StateError('--count must be positive; labels are allowed for one agent only.')
    ca = native_tail if native_tail is not None else a.codex_args
    if a.label and b.console.by_label(a.label):
        raise StateError('That label already exists. Use cx focus LABEL instead; nothing created.')
    mode = 'tab' if a.tab else 'window' if a.window else 'split'
    anchor = cx_iterm.caller_tty()
    gui = cx_iterm.ITerm()
    gui.configure(b.store.preference('timestamps', True))
    gui.preflight(mode, anchor)
    created = []
    try:
        for _ in range(a.count):
            created.append(b.console.start_agent(ca, a.label, detached=True))
    except (RuntimeError, OSError) as exc:
        raise StateError(f'Partial launch ({len(created)} recorded). Inspect cxl; successful sessions were kept. {exc}') from exc
    rows = b.snapshot()['sessions']
    selected = [r for s in created for r in rows if (r['sid'], r['created']) == (s['sid'], s['created'])]
    if len(selected) != len(created):
        raise StateError('Created sessions changed before GUI layout; inspect cxl. Nothing terminated.')
    return focus_rows(selected, b, mode, gui=gui, anchor=anchor)


def launch_selected(chosen, history, args, b):
    """Revalidate the entire selection under the existing resume launcher lock."""
    r = native()
    if not chosen:
        print('No sessions selected; nothing started.')
        return
    opts = layout(args)
    gui = cx_iterm.ITerm()
    if not args.no_iterm:
        gui.configure(b.store.preference('timestamps', True))
        gui.preflight()
    with r.launch_lock(r.codex_home()):
        fresh, unknown, _, failed = catalog(b, history)
        index = {x['key']: x for x in fresh}
        selected = [index.get(x['key']) for x in chosen]
        if any(x is None for x in selected):
            raise StateError('A selected session disappeared. Nothing started; refresh the picker.')
        for old, now in zip(chosen, selected):
            if old.get('managed') and not old.get('thread_id'):
                if not now.get('managed') or old['managed']['_key'] != now['managed']['_key']:
                    raise StateError('Selected live-session generation changed; nothing started.')
        if any(x['external_pids'] for x in selected):
            details = {x['thread_id']: x['external_pids'] for x in selected if x['external_pids']}
            raise StateError(f'Selected conversation has a known external live Codex copy {details}. Exit that exact copy first.')
        if any(x.get('managed', {}).get('state') == 'UNKNOWN' for x in selected if x.get('managed')):
            raise StateError('Selected session liveness is unknown; no duplicate launched.')
        cold = [x for x in selected if x['thread_id'] and
                (not x.get('managed') or x['managed']['state'] not in ('ALIVE', 'STOPPED'))]
        if cold and (unknown or failed) and not args.allow_unverified_live:
            raise StateError(f'Unidentified live Codex PIDs {unknown or "UNKNOWN"} cannot be mapped to UUIDs and could duplicate a cold resume. Inspect cx doctor, stop them, or pass --allow-unverified-live to accept this uncertainty.')
        for item in cold:
            r.planned_cwd(item, args.cwd)
        results, result_titles, errors = [], {}, []
        for item in selected:
            try:
                session, action = r.ensure(item, b, args.cwd, yolo=args.yolo)
                results.append(session)
                result_titles[b.normalize_generation(session)] = item['title']
                print(f"{action}: {b.clean(item['title'])}")
            except (RuntimeError, OSError) as exc:
                errors.append(str(exc))
        current = b.snapshot()['sessions']
        rows = [x for s in results for x in current
                if b.normalize_generation(x) == b.normalize_generation(s)]
        for row in rows:
            row['display_name'] = result_titles.get(b.normalize_generation(row), row.get('display_name'))
        if len(rows) != len(results):
            errors.append('A session changed before layout; inspect cxl.')
        if args.no_iterm:
            for row in rows:
                print('Attach: cxa ' + row['session'])
        elif rows:
            focus_rows(rows, b, gui=gui, **opts)
        if errors:
            raise StateError('Partial operation; successful agents kept. ' + ' | '.join(errors))
    if not args.no_dashboard:
        dashboard(b)


def config_command(argv, b):
    parser = argparse.ArgumentParser(prog='cx config')
    parser.add_argument('setting', choices=('timestamps',))
    parser.add_argument('value', nargs='?', choices=('on', 'off'))
    args = parser.parse_args(argv)
    if args.value is None:
        enabled = b.store.preference('timestamps', True)
        print('timestamps: ' + ('on' if enabled else 'off'))
        return 0
    enabled = args.value == 'on'
    b.store.set_preference('timestamps', enabled)
    if sys.platform != 'darwin':
        print(f'timestamps: {args.value} (saved; applies when CX Deck creates iTerm2 views on macOS)')
        print('Running agents were unchanged.')
        return 0
    try:
        path, changed = cx_iterm.ensure_profile(enabled)
    except (RuntimeError, OSError) as exc:
        raise StateError('Timestamp preference was saved, but the CX Deck iTerm2 profile could not be updated. '
                         'Running agents were unchanged. ' + str(exc)) from exc
    print(f"timestamps: {args.value} ({'updated' if changed else 'already current'}: {path})")
    print('Only CX Deck-created iTerm2 views use this profile. Running agents were unchanged.')
    return 0


def resume(args, b):
    r = native()
    layout(args)
    if not 1 <= args.limit <= 2000:
        raise StateError('--limit must be between 1 and 2000.')
    warning = None
    try:
        history, truncated = r.list_history(r.codex_home(), args.limit)
    except RuntimeError as exc:
        history, truncated, warning = [], False, str(exc)
    rows, _, warnings, _ = catalog(b, history)
    if getattr(args, 'group', None):
        if args.group == '@ungrouped':
            rows = [row for row in rows if row['group_id'] is None]
        else:
            group_id, _ = b.store.resolve_group(args.group)
            rows = [row for row in rows if row['group_id'] == group_id]
    if warning:
        if not rows:
            raise StateError(warning)
        warnings.insert(0, 'Saved history unavailable; managed sessions only: ' + warning)
    if truncated:
        warnings.append(f'History capped at {args.limit}; increase --limit for older entries.')
    if args.json:
        print(json.dumps(dict(rows=rows, warnings=warnings, truncated=truncated), indent=2))
        return
    if args.list:
        for row in rows:
            print(f"{row['key']}  {row['state']:12}  {b.clean(row['title'])}")
        for message in warnings:
            print('WARNING: ' + b.clean(message))
        return
    if args.select:
        index = {x['key']: x for x in rows}
        if any(key not in index for key in args.select):
            raise StateError('Selection not in loaded history/sessions. Use --list or increase --limit.')
        chosen = [index[key] for key in dict.fromkeys(args.select)]
    else:
        chosen = r.choose(rows, warnings) if rows else []
    return launch_selected(chosen, history, args, b)


def group_command(argv, b):
    parser = argparse.ArgumentParser(prog='cx group')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('name')
    sub.add_parser('list')
    rename = sub.add_parser('rename')
    rename.add_argument('group')
    rename.add_argument('name')
    delete = sub.add_parser('delete')
    delete.add_argument('group')
    add = sub.add_parser('add')
    add.add_argument('group')
    add.add_argument('conversation', nargs='+')
    remove = sub.add_parser('remove')
    remove.add_argument('conversation', nargs='+')
    args = parser.parse_args(argv)
    if args.action == 'create':
        ident = b.store.create_group(args.name)
        print(f'Created group {b.clean(args.name)} ({ident}).')
        return
    if args.action == 'list':
        groups = sorted(b.store.read()['groups'].items(), key=lambda item: item[1].get('order', 0))
        for ident, record in groups:
            print(f"{ident}  {b.clean(record.get('name', '?'))}")
        return
    if args.action == 'rename':
        b.store.rename_group(args.group, args.name)
        print('Group renamed. Conversation and workspace identity unchanged.')
        return
    if args.action == 'delete':
        b.store.delete_group(args.group)
        print('Group deleted; conversations are now Ungrouped. No Codex history changed.')
        return
    history, _ = native().list_history(native().codex_home(), 2000)
    rows = catalog(b, history)[0]
    index = {}
    for row in rows:
        if row.get('thread_id'):
            index[row['thread_id']] = row
        if row.get('managed'):
            index[row['managed']['session']] = row
    selected = []
    for token in args.conversation:
        row = index.get(token)
        if not row or not row.get('thread_id'):
            raise StateError('Group assignment requires an exact UUID or exact managed session with a verified UUID.')
        selected.append(thread_key(row['home'], row['thread_id'], socket.gethostname()))
    group_id = b.store.resolve_group(args.group)[0] if args.action == 'add' else None
    b.store.assign_group(list(dict.fromkeys(selected)), group_id)
    print(f"Updated group membership for {len(set(selected))} conversation(s). No runtime changed.")


def workspace_save(title, b, tokens=None, replace=False, settings=None):
    title = name(title, 80)
    data = b.snapshot()
    rows = data['sessions'] if tokens is None else [resolve(t, b) for t in tokens]
    rows = list({r['_key']: r for r in rows}.values())
    if not rows:
        raise StateError('No sessions selected for this workspace; nothing saved.')
    observed = catalog(b)[0]
    members = []
    for row in rows:
        obs = next((x for x in observed if x.get('managed') and x['managed']['_key'] == row['_key']), {})
        tid = obs.get('thread_id') or row.get('thread_id') or None
        home = row.get('codex_home') or native().codex_home()
        members.append(dict(live_key=row['_key'], session=row['session'], thread_id=tid,
            home=home, title=row['display_name'], cwd=row.get('launch_cwd'), pinned=row['pinned']))
    record = dict(host=data['context']['host'], source_context=data['context'], saved_at=time.time(), members=members,
                  layout=settings or dict(per_tab=0, min_columns=70, min_rows=12))
    b.store.workspace(title, record, replace)
    for item in members:
        b.store.annotate([thread_key(item['home'], item['thread_id'], record['host'])],
                         name=item['title'], pinned=item['pinned'])
    unknown = sum(not x['thread_id'] for x in members)
    print(f'Saved workspace {title}: {len(members)} sessions. {unknown} are live-only (no verified conversation ID).')
    return record


def workspace_plan(record, b):
    if record.get('host') != socket.gethostname() or not isinstance(record.get('members'), list):
        raise StateError('Workspace belongs to another host or has invalid metadata; no remote actions attempted.')
    home = native().codex_home()
    history, members, missing = [], [], []
    for item in record['members']:
        if not isinstance(item, dict) or not isinstance(item.get('home'), str):
            raise StateError('Invalid workspace member; nothing launched.')
        title = name(item.get('title'))
        if os.path.realpath(item['home']) != home:
            missing.append(title + ': different CODEX_HOME')
            continue
        members.append(item)
        if item.get('thread_id'):
            tid = native().thread_id(item['thread_id'])
            history.append(dict(key=tid, thread_id=tid, title=title, cwd=item.get('cwd'),
                home=home, updated=record.get('saved_at', 0), managed=None, state='SAVED', external_pids=[]))
    available = catalog(b, history)[0]
    rows, seen = [], set()
    for item in members:
        live = next((x for x in available if x.get('managed') and
                     x['managed']['_key'] == item.get('live_key')), None)
        saved = next((x for x in available if item.get('thread_id') and
                      x['thread_id'] == item['thread_id']), None)
        # Missing fresh UUID evidence must NOT turn one surviving process into
        # both a cold resume and a live attachment. Prefer its verified generation.
        chosen = saved if saved and saved.get('managed') else live if live and not live['thread_id'] else saved or live
        if chosen is None:
            missing.append(item['title'] + ': live-only session is gone; no cold identity to guess')
        elif chosen['key'] not in seen:
            seen.add(chosen['key'])
            rows.append(chosen)
    return rows, history, missing


def workspace_command(argv, b):
    p = argparse.ArgumentParser(prog='cx workspace')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    save = sub.add_parser('save')
    save.add_argument('name')
    save.add_argument('--select', nargs='+')
    save.add_argument('--replace', action='store_true')
    save.add_argument('--per-tab', type=int, default=0)
    save.add_argument('--min-columns', type=int, default=70)
    save.add_argument('--min-rows', type=int, default=12)
    op = sub.add_parser('open')
    op.add_argument('name')
    op.add_argument('--all', action='store_true', help='explicitly open all available members, without picker')
    op.add_argument('--list', action='store_true', help='preview only; no launches')
    op.add_argument('--no-iterm', action='store_true')
    op.add_argument('--no-dashboard', action='store_true')
    op.add_argument('--cwd')
    op.add_argument('--per-tab', type=int)
    op.add_argument('--min-columns', type=int)
    op.add_argument('--min-rows', type=int)
    native().add_launch_policy_arguments(op)
    a = p.parse_args(argv)
    if a.action == 'save':
        return workspace_save(a.name, b, a.select, a.replace, layout(a))
    saved = b.store.read()['workspaces']
    if a.action == 'list':
        for title, record in saved.items():
            print(f"{b.clean(title)}  {len(record.get('members', []))} sessions  host={b.clean(record.get('host', '?'))}")
        return
    if a.name not in saved:
        raise StateError('No workspace with this exact name. Use cx workspace list.')
    rows, history, missing = workspace_plan(saved[a.name], b)
    for text in missing:
        print('Unavailable: ' + b.clean(text))
    if a.list:
        for row in rows:
            print(f"{row['state']:12} {b.clean(row['title'])}  {row['key']}")
        return
    if a.all and missing:
        raise StateError('Workspace is incomplete. Review without --all to select available members; nothing started.')
    settings = saved[a.name].get('layout', {})
    for key, default in (('per_tab', 0), ('min_columns', 70), ('min_rows', 12)):
        if getattr(a, key) is None:
            setattr(a, key, settings.get(key, default))
    a.allow_unverified_live = False
    chosen = rows if a.all else native().choose(rows, missing) if rows else []
    return launch_selected(chosen, history, a, b)


def text_lines(data):
    lines = [f"CX Deck {VERSION} | zmx | {data['context']['host']} | {len(data['sessions'])} agents"]
    for row in sorted(data['sessions'], key=lambda r: (not r['pinned'], r['display_name'].casefold())):
        lines.append(f"{'*' if row['pinned'] else ' '} {row['display_name']} | {row['state']} | clients={row['attached']} | PID={','.join(map(str, row['codex_pids'])) or '-'}")
        lines.append('  ' + row['session'])
    lines.append('ALIVE = process exists, not task progress. No automatic task instructions.')
    lines.extend('WARNING: ' + str(w) for w in data.get('warnings', []))
    return [''.join(c if c.isprintable() else '?' for c in s) for s in lines]


def normalized_status(data, installed_version=VERSION):
    """Stable JSON view with explicit zmx and runtime-compatibility fields."""
    from cx_upgrade import runtime_compatibility
    output = copy.deepcopy(data)
    context = output.get('context', {})
    for row in output.get('sessions', []):
        row['backend'] = row.get('backend') or context.get('backend') or 'UNKNOWN'
        row['runtime_version'] = row.get('runtime_version') or context.get('runtime_version') or 'UNKNOWN'
        row['thread_id'] = row.get('thread_id') or 'UNKNOWN'
        row['launch_policy'] = row.get('launch_policy') or 'UNKNOWN'
        row['launch_mode'] = row.get('launch_mode') or 'UNKNOWN'
        row['cx_version'] = row.get('cx_version') or row.get('labels', {}).get('cx_version') or 'UNKNOWN'
        row['upgrade_state'] = runtime_compatibility(row['cx_version'], installed_version)
        row['state'] = row.get('state') or 'UNKNOWN'
    return output


def dashboard(b, interval=3, once=False, as_json=False):
    if not math.isfinite(interval) or interval < 1:
        raise StateError('Refresh interval must be finite and at least one second.')
    if once or as_json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        data = b.snapshot()
        installed = getattr(b, 'VERSION', VERSION)
        installed = installed if isinstance(installed, str) else VERSION
        print(json.dumps(normalized_status(data, installed), indent=2)
              if as_json else '\n'.join(text_lines(data)))
        return
    def screen(win):
        win.keypad(True)
        win.timeout(200)
        selected, cursor, query, editing = set(), None, '', False
        data, message, refresh_at = None, '', 0
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        def external(action):
            curses.def_prog_mode()
            curses.endwin()
            try:
                action()
                return 'Action complete; agent processes retained.'
            except (RuntimeError, OSError, ValueError) as exc:
                return str(exc)
            finally:
                curses.reset_prog_mode()
                win.clear()
                win.timeout(200)
        def ask(prompt):
            return input(prompt).strip()
        while True:
            if time.monotonic() >= refresh_at:
                try:
                    data = b.snapshot()
                except (RuntimeError, OSError) as exc:
                    message = 'Refresh failed; actions disabled: ' + str(exc)
                    data = None
                refresh_at = time.monotonic() + interval
            rows = sorted((data or {}).get('sessions', []), key=lambda r: (not r['pinned'], r['display_name'].casefold()))
            rows = [r for r in rows if query.casefold() in (r['display_name'] + ' ' + r['session'] + ' ' + str(r.get('thread_id', ''))).casefold()]
            keys = [r['_key'] for r in rows]
            if cursor not in keys:
                cursor = keys[0] if keys else None
            pos = keys.index(cursor) if cursor else 0
            h, w = win.getmaxyx()
            def put(y, value, attr=0):
                if 0 <= y < h and w > 1:
                    try:
                        win.addstr(y, 0, native().clip(value, w - 1), attr)
                    except curses.error:
                        pass
            win.erase()
            put(0, f'CX Deck {VERSION} | {len(rows)} visible | {len(selected)} selected', curses.A_BOLD)
            put(1, 'n: new split | N: multiple | Enter: focus/open | Space: select | /: search')
            put(2, 'r: rename | p: pin | s: save workspace | w: open workspace | q: quit view')
            put(3, 'Search: ' + query + ('_' if editing else ''))
            page = max(1, h - 7)
            offset = (pos // page) * page
            for i, row in enumerate(rows[offset:offset + page], offset):
                pid = ','.join(map(str, row['codex_pids'])) or '-'
                put(4 + i - offset, f"{'[x]' if row['_key'] in selected else '[ ]'} {'*' if row['pinned'] else ' '} {row['display_name']} | {row['state']} | {row['attached']} views | PID {pid}", curses.A_REVERSE if row['_key'] == cursor else 0)
            put(h - 2, message or 'ALIVE means process exists; it does not mean task complete.')
            put(h - 1, 'No research repo required. q / Ctrl-C closes only this console.')
            win.refresh()
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if editing:
                if key in ('\n', '\r', '\x1b'):
                    editing = False
                elif key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                    query = query[:-1]
                elif isinstance(key, str) and key.isprintable():
                    query += key
                continue
            if key in ('q', '\x1b'):
                return
            if key == '/':
                editing = True
            elif key in ('j', curses.KEY_DOWN) and keys:
                cursor = keys[min(pos + 1, len(keys) - 1)]
            elif key in ('k', curses.KEY_UP) and keys:
                cursor = keys[max(pos - 1, 0)]
            elif key == ' ' and cursor:
                selected.symmetric_difference_update([cursor])
            elif key in ('n', 'N') and data is not None:
                def create():
                    count = int(ask('How many new agents? ')) if key == 'N' else 1
                    new_agents(['--split', '--count', str(count)], b)
                message = external(create)
                refresh_at = 0
            elif key == 'w' and data is not None:
                message = external(lambda: workspace_command(['open', ask('Workspace name: '), '--no-dashboard'], b))
                refresh_at = 0
            elif cursor and data is not None:
                current = rows[pos]
                targets = [r for r in data['sessions'] if r['_key'] in selected] if selected else [current]
                stale = selected - {r['_key'] for r in data['sessions']}
                if stale and key in ('\n', '\r', 's'):
                    message = 'A selected session disappeared. Selection cleared; choose again.'
                    selected.clear()
                    continue
                if key in ('\n', '\r'):
                    message = external(lambda: focus_rows(targets, b))
                elif key == 'r':
                    message = external(lambda: annotate(current['session'], b, title=ask('New display name: ')))
                elif key == 'p':
                    message = external(lambda: annotate(current['session'], b, pinned=not current['pinned']))
                elif key == 's':
                    message = external(lambda: workspace_save(ask(f'Save {len(targets)} selected session(s) as: '), b, [r['session'] for r in targets]))
                else:
                    continue
                refresh_at = 0
    try:
        return curses.wrapper(screen)
    except curses.error as exc:
        raise StateError('Cannot initialize interactive dashboard. Use cxl or cx status --json.') from exc
