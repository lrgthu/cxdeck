"""Workbench over zmx conversations and native iTerm views.

No prompts, approvals, automatic commits, process kills or remote commands.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import curses
import json
import math
import os
import socket
import sys
import time
import types

from cx_store import Store, StateError, live_key, thread_key, name
import cx_iterm
import cx_inventory
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


def runtime_snapshot(b, *, bind_threads=True):
    """Take one runtime snapshot, optionally forbidding discovery-label writes."""
    if bind_threads or not callable(getattr(type(b), 'raw_snapshot', None)):
        return b.snapshot()
    procs = b.processes()
    return enrich(b.raw_snapshot(bind_threads=False, process_table=procs,
                                 include_diagnostics=False), b, b.store)


def resolve(token, b, *, bind_threads=True):
    rows = runtime_snapshot(b, bind_threads=bind_threads)['sessions']
    exact = [r for r in rows if r['session'] == token]
    matches = exact or [r for r in rows if token in (r['task'], r.get('display_name'), r.get('thread_id'))]
    if len(matches) != 1:
        raise StateError('Session name/label must match exactly and unambiguously. Use cxl for full names.')
    return matches[0]


def catalog(b, history=(), *, bind_threads=True):
    r = native()
    if bind_threads:
        provider = b
    else:
        procs = b.processes()
        raw = enrich(b.raw_snapshot(bind_threads=False, process_table=procs,
                                    include_diagnostics=False), b, b.store)
        provider = types.SimpleNamespace(snapshot=lambda: copy.deepcopy(raw))
    rows, unknown, warnings, failed = r.inventory(history, r.codex_home(), provider)
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
    row = resolve(token, b, bind_threads=False)
    observed = next((x for x in catalog(b, bind_threads=False)[0]
                     if (x.get('managed') or {}).get('_key') == row['_key']), None)
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


def _read_history(limit=2000):
    r = native()
    try:
        history, truncated = r.list_history(r.codex_home(), limit)
        warning = None
    except (RuntimeError, OSError) as exc:
        history, truncated, warning = [], False, 'Saved history unavailable: ' + str(exc)
    return history, truncated, warning


def inventory_snapshot(b, history=None, gui=None, history_warning=None):
    """Collect each external source once, then compose without changing reality."""
    r = native()
    if history is None:
        history, _, history_warning = _read_history()
    client_error, procs = None, None
    try:
        procs = b.processes()
    except (RuntimeError, OSError) as exc:
        client_error = exc
    raw = b.raw_snapshot(bind_threads=False, process_table=procs, include_diagnostics=False)
    proxy = types.SimpleNamespace(snapshot=lambda: copy.deepcopy(raw))
    rows, unknown, warnings, failed = r.inventory(history, r.codex_home(), proxy)
    if client_error:
        clients = {row.get('sid'): None for row in raw.get('sessions', [])}
    else:
        try:
            clients = {sid: sorted(ttys) for sid, ttys in
                       b.client_tty_map(raw.get('sessions', []), procs).items()}
        except (RuntimeError, OSError) as exc:
            client_error = exc
            clients = {row.get('sid'): None for row in raw.get('sessions', [])}
    view_error, views = None, None
    gui = gui or cx_iterm.ITerm()
    try:
        views = gui.inventory()
    except (RuntimeError, OSError) as exc:
        view_error = exc
    if history_warning:
        warnings = [history_warning, *warnings]
    return cx_inventory.compose(
        history, rows, raw.get('context', {}), b.store.read(), clients,
        iterm_views=views, view_error=view_error, client_error=client_error,
        unknown_pids=unknown, process_failed=failed, warnings=warnings,
        timestamp=raw.get('timestamp'), process_table=procs)


def _workspace_inventory(snapshot, store_state, workspace_name):
    if workspace_name not in store_state.get('workspaces', {}):
        raise StateError('No workspace with this exact name. Use cx workspace list.')
    workspace = store_state['workspaces'][workspace_name]
    if workspace.get('host') != snapshot.get('host'):
        raise StateError('Workspace belongs to another host; no presentation action was changed.')
    identities = []
    members = workspace.get('members')
    if not isinstance(members, list):
        raise StateError('Workspace membership metadata is invalid.')
    for member in members:
        if not isinstance(member, dict) or not member.get('home') or not member.get('thread_id'):
            raise StateError('Workspace-scoped view commands require exact UUID-backed members.')
        identities.append((workspace['host'], os.path.realpath(member['home']), member['thread_id']))
    if len(identities) != len(set(identities)):
        raise StateError('Workspace contains duplicate conversation identities.')
    index = {(record['identity']['host'], record['identity']['codex_home'],
              record['identity']['thread_id']): record
             for record in snapshot['conversations'] if record.get('identity')}
    missing = [identity[2] for identity in identities if identity not in index]
    if missing:
        raise StateError('Workspace conversation is absent from current inventory: ' + ', '.join(missing))
    return [index[identity] for identity in identities]


def _view_records(snapshot, b, workspace=None):
    records = snapshot['conversations']
    if workspace:
        records = _workspace_inventory(snapshot, b.store.read(), workspace)
    else:
        records = [record for record in records if record['runtime']['managed']]
    return records


def _print_view_status(records, b):
    print(f"{'VIEW':20} {'NAME':32} {'RUNTIME':12} CLIENTS")
    for record in records:
        print(f"{record['view_state']:20} {b.clean(record['display']['name'])[:32]:32} "
              f"{record['runtime_state']:12} {record['view']['attached_count']}")


def _verified_receipts(records):
    return {record['runtime']['live_key']: {'guid': record['view']['guid'], 'tty': record['view']['tty']}
            for record in records if record['runtime'].get('live_key') and record['view'].get('verified')}


def views_command(argv, b, *, gui=None):
    parser = argparse.ArgumentParser(prog='cx views')
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')
    status.add_argument('--workspace')
    rebuild = sub.add_parser('rebuild')
    rebuild.add_argument('--workspace')
    refresh = sub.add_parser('refresh')
    refresh.add_argument('--workspace')
    args = parser.parse_args(argv)
    history, _, history_warning = _read_history()
    observe = lambda: inventory_snapshot(b, history=history, gui=gui,
                                         history_warning=history_warning)
    snapshot = observe()
    records = _view_records(snapshot, b, args.workspace)
    if args.command == 'status':
        if args.json:
            output = cx_inventory.public(snapshot)
            selected = {record['key'] for record in records}
            output['conversations'] = [item for item in output['conversations']
                                       if item['key'] in selected]
            output['workspace'] = args.workspace
            print(json.dumps(output, indent=2))
        else:
            _print_view_status(records, b)
            for warning in snapshot['warnings']:
                print('WARNING: ' + b.clean(warning))
        return 0
    if not args.workspace:
        records = [record for record in records if record['runtime_state'] in ('ALIVE', 'STOPPED')]
    ambiguous = [record for record in records if record['view_state'] in
                 ('MULTIPLE_CLIENTS', 'UNVERIFIED_CLIENT', 'CHANGED_GENERATION', 'VIEW_UNKNOWN')]
    unavailable = [record for record in records if not record['runtime']['managed'] or
                   record['runtime_state'] not in ('ALIVE', 'STOPPED')]
    if ambiguous:
        raise StateError('Ambiguous presentation blocks this operation: ' + ', '.join(
            record['display']['name'] + '=' + record['view_state'] for record in ambiguous))
    if unavailable:
        raise StateError('Presentation rebuild never starts runtimes; no live managed generation for: ' +
                         ', '.join(record['display']['name'] for record in unavailable))
    rows = [dict(record['_managed'], display_name=record['display']['name']) for record in records]
    gui = gui or cx_iterm.ITerm()
    if args.command == 'refresh':
        refresh_rows = [row for row, record in zip(rows, records) if record['view'].get('verified')]
        result = (cx_iterm.refresh(refresh_rows, b, b.store, gui=gui)
                  if refresh_rows else dict(refreshed=0, missing=0))
        result['missing'] += len(records) - len(refresh_rows)
        fresh = observe()
        fresh_records = _view_records(fresh, b, args.workspace)
        changed = [record for record in fresh_records if record['view_state'] in
                   ('MULTIPLE_CLIENTS', 'UNVERIFIED_CLIENT', 'CHANGED_GENERATION', 'VIEW_UNKNOWN')]
        if changed:
            raise StateError('Presentation changed during refresh; runtimes remain unchanged. ' + ', '.join(
                record['display']['name'] + '=' + record['view_state'] for record in changed))
        receipts = _verified_receipts(fresh_records)
        if receipts:
            b.store.set_view_receipts(receipts)
        print(f"Presentation refreshed: {result['refreshed']}; no verified view: {result['missing']}. "
              f"Receipts verified: {len(receipts)}. No agent restarted.")
        return 0
    result = focus_rows(rows, b, mode='window', gui=gui) if rows else dict(opened=0, reused=0)
    fresh = observe()
    fresh_records = _view_records(fresh, b, args.workspace)
    still_bad = [record for record in fresh_records if record['view_state'] not in
                 ('VERIFIED_VIEW', 'STALE_RECEIPT')]
    if still_bad:
        raise StateError('Presentation operation was partial; runtimes remain unchanged. ' + ', '.join(
            record['display']['name'] + '=' + record['view_state'] for record in still_bad))
    receipts = _verified_receipts(fresh_records)
    if receipts:
        b.store.set_view_receipts(receipts)
    print(f"Verified zmx targets: {len(rows)}. Views opened: {result['opened']}; reused: {result['reused']}. "
          f"Scope: {args.workspace or 'all managed conversations'}.")
    return 0


def find_command(argv, b):
    parser = argparse.ArgumentParser(prog='cx find')
    parser.add_argument('query')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        found = cx_inventory.search(inventory_snapshot(b)['conversations'], args.query)
    except cx_inventory.InventoryError as exc:
        raise StateError(str(exc)) from exc
    if args.json:
        print(json.dumps({'schema': cx_inventory.SCHEMA, 'query': args.query,
                          'matches': cx_inventory.public({'conversations': found})['conversations']}, indent=2))
    else:
        for record in found:
            tid = (record.get('identity') or {}).get('thread_id') or 'UNBOUND'
            print(f"{tid}  {record['conversation_state']:28} {record['runtime_state']:10} "
                  f"{record['view_state']:18} {b.clean(record['display']['name'])}")
        if not found:
            print('No metadata matches.')
    return 0


def focus_navigation(direction, b, *, gui=None, caller_tty=None):
    if direction not in ('next', 'previous'):
        raise StateError('Navigation direction must be next or previous.')
    gui = gui or cx_iterm.ITerm()
    history, _, history_warning = _read_history()
    observe = lambda: inventory_snapshot(b, history=history, gui=gui,
                                         history_warning=history_warning)
    snapshot = observe()
    candidates = [record for record in snapshot['conversations'] if record['view'].get('verified')]
    if not candidates:
        raise StateError('No uniquely verified CX Deck views are available to focus.')
    caller = cx_iterm.caller_tty() if caller_tty is None else caller_tty
    current = [index for index, record in enumerate(candidates) if record['view']['tty'] == caller]
    if len(current) > 1:
        raise StateError('Caller TTY maps to multiple conversations; no view focused.')
    if current:
        index = (current[0] + (1 if direction == 'next' else -1)) % len(candidates)
    else:
        index = 0 if direction == 'next' else len(candidates) - 1
    chosen = candidates[index]
    fresh = observe()
    def identity(record):
        durable = record.get('identity')
        if durable:
            return ('thread', durable['host'], durable['codex_home'], durable['thread_id'])
        return ('generation', record.get('runtime', {}).get('live_key'))
    matches = [record for record in fresh['conversations'] if identity(record) == identity(chosen)]
    if (len(matches) != 1 or not matches[0]['view'].get('verified') or
            (matches[0]['view']['guid'], matches[0]['view']['tty']) !=
            (chosen['view']['guid'], chosen['view']['tty']) or
            matches[0]['runtime'].get('generation') != chosen['runtime'].get('generation')):
        raise StateError('Selected view changed during navigation; no new client was opened.')
    gui.focus({'guid': chosen['view']['guid'], 'tty': chosen['view']['tty']})
    print(f"Focused {b.clean(chosen['display']['name'])}; no client or runtime created.")
    return chosen


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
    if args.list or args.json:
        snapshot = inventory_snapshot(b, history=history)
        if warning:
            snapshot['warnings'].insert(0, 'Saved history unavailable; managed sessions only: ' + warning)
        if truncated:
            snapshot['warnings'].append(f'History capped at {args.limit}; increase --limit for older entries.')
        records = snapshot['conversations']
        if getattr(args, 'group', None):
            if args.group == '@ungrouped':
                records = [record for record in records if record['display']['group_id'] is None]
            else:
                group_id, _ = b.store.resolve_group(args.group)
                records = [record for record in records if record['display']['group_id'] == group_id]
        if args.json:
            output = cx_inventory.public(snapshot)
            keys = {record['key'] for record in records}
            output['conversations'] = [record for record in output['conversations'] if record['key'] in keys]
            output['history_truncated'] = truncated
            print(json.dumps(output, indent=2))
        else:
            for record in records:
                print(f"{record['key']}  {record['conversation_state']:28} "
                      f"{record['runtime_state']:10} {b.clean(record['display']['name'])}")
            for message in snapshot['warnings']:
                print('WARNING: ' + b.clean(message))
        return
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
    rows = catalog(b, history, bind_threads=False)[0]
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
    data = runtime_snapshot(b, bind_threads=False)
    rows = (data['sessions'] if tokens is None else
            [resolve(t, b, bind_threads=False) for t in tokens])
    rows = list({r['_key']: r for r in rows}.values())
    if not rows:
        raise StateError('No sessions selected for this workspace; nothing saved.')
    observed = catalog(b, bind_threads=False)[0]
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


def workspace_plan(record, b, *, bind_threads=True):
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
    available = catalog(b, history, bind_threads=bind_threads)[0]
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


def _capture_runtime_observation(b):
    """Read zmx/process/view facts without binding labels or changing a runtime."""
    procs = b.processes()
    data = enrich(b.raw_snapshot(bind_threads=False, process_table=procs,
                                 include_diagnostics=False), b, b.store)
    for row in data['sessions']:
        sid = row.get('sid')
        if not isinstance(sid, str) or not sid:
            raise StateError('Managed zmx session is missing its exact runtime ID.')
    clients = {sid: sorted(ttys) for sid, ttys in
               b.client_tty_map(data['sessions'], procs).items()}
    state = b.store.read()
    return {'context': data['context'], 'sessions': data['sessions'],
            'clients': clients, 'views': copy.deepcopy(state['views'])}


def workspace_capture(title, b, replace=False, provider=None, attempts=3):
    """Capture exact presentation topology, then atomically update one workspace."""
    from cx_workspace_layout import (LayoutError, capture_contract, capture_live,
                                     conversation_identities)
    title = name(title, 80)
    state = b.store.read()
    if title not in state['workspaces']:
        raise StateError('No workspace with this exact name. Use cx workspace list.')
    expected = copy.deepcopy(state['workspaces'][title])
    if expected.get('exact_layout') is not None and not replace:
        raise StateError('Workspace already has an exact layout. Use --replace deliberately; nothing overwritten.')
    reader = lambda: _capture_runtime_observation(b)
    try:
        result = (asyncio.run(capture_contract(expected, provider, reader, attempts=attempts))
                  if provider is not None else capture_live(expected, reader, attempts=attempts))
    except LayoutError as exc:
        raise StateError(str(exc)) from exc
    b.store.set_workspace_layout(title, result.layout, expected, result.store_views,
                                 replace=replace)
    identities = conversation_identities(result.layout)
    windows = result.layout['windows']
    tabs = sum(len(window['tabs']) for window in windows)
    print('Captured exact workspace layout:\n'
          f'  workspace: {b.clean(title)}\n'
          f'  conversations: {len(identities)}\n'
          f'  windows: {len(windows)}\n'
          f'  tabs: {tabs}\n'
          '  topology: L3\n'
          '  ratios: approximate')
    return result.layout


def _exact_runtime_observation(b, history, home):
    """One nonbinding global runtime/process/client observation for exact restore."""
    procs = b.processes()
    raw = enrich(b.raw_snapshot(bind_threads=False, process_table=procs,
                                include_diagnostics=False), b, b.store)
    proxy = types.SimpleNamespace(snapshot=lambda: copy.deepcopy(raw))
    rows, unknown, warnings, failed = native().inventory(history, home, proxy)
    sessions = []
    for item in rows:
        if not item.get('managed'):
            continue
        row = copy.deepcopy(item['managed'])
        if item.get('thread_id'):
            row['thread_id'] = item['thread_id']
            row['codex_home'] = item['home']
        row['external_pids'] = list(item.get('external_pids') or [])
        sessions.append(row)
    clients = {sid: sorted(ttys) for sid, ttys in b.client_tty_map(sessions, procs).items()}
    return {'context': raw['context'], 'sessions': sessions, 'clients': clients,
            'views': copy.deepcopy(b.store.read()['views']), 'inventory': rows,
            'unknown_pids': unknown, 'process_failed': failed, 'warnings': warnings}


def workspace_open_exact(title, record, args, b):
    """Resolve all runtimes, then MOVE_REUSE native presentation and verify it."""
    import cx_workspace_restore as restore
    r = native()
    exact_layout = record.get('exact_layout')
    try:
        restore.validate_layout(exact_layout)
    except restore.LayoutError as exc:
        raise StateError('RUNTIME_BLOCKED: invalid saved exact layout: ' + str(exc)) from exc
    home = r.codex_home()
    history_error = None
    try:
        history, _ = r.list_history(home, 2000)
    except (RuntimeError, OSError) as exc:
        history, history_error = [], str(exc)

    def observe():
        return _exact_runtime_observation(b, history, home)

    first = observe()
    try:
        plan = restore.build_runtime_plan(
            exact_layout, record, first['inventory'], first['unknown_pids'],
            first['process_failed'], host=socket.gethostname(), codex_home=home,
            allow_unverified_live=args.allow_unverified_live, cwd_override=args.cwd,
            planned_cwd=r.planned_cwd, context=first['context'])
    except restore.RestoreError as exc:
        detail = (' Saved history was unavailable: ' + history_error
                  if history_error and 'absent from saved history' in str(exc) else '')
        raise StateError(str(exc) + detail) from exc

    if getattr(args, 'list', False):
        for item in plan.items:
            print(f'{item.classification:14} {b.clean(item.display_name)}  {item.identity[2]}')
        print(f'Exact topology: {len(plan.layout["windows"])} window(s); '
              f'{sum(len(window["tabs"]) for window in plan.layout["windows"])} tab(s). No action taken.')
        return plan

    # The complete presentation blueprint and caller conflict are checked before
    # the first saved-only runtime is created.
    if not args.no_iterm:
        try:
            restore.preflight_live(plan.layout, plan.items, observe, first['context'],
                                   cx_iterm.caller_tty())
        except restore.RestoreError as exc:
            raise StateError(str(exc)) from exc

    class ResumeFacade:
        VERSION = b.VERSION
        store = None
        def __getattr__(self, key):
            return getattr(b, key)

    launched, failures = [], []
    with r.launch_lock(home):
        locked = observe()
        try:
            plan = restore.build_runtime_plan(
                exact_layout, record, locked['inventory'], locked['unknown_pids'],
                locked['process_failed'], host=socket.gethostname(), codex_home=home,
                allow_unverified_live=args.allow_unverified_live, cwd_override=args.cwd,
                planned_cwd=r.planned_cwd, context=locked['context'])
        except restore.RestoreError as exc:
            raise StateError(str(exc)) from exc
        cold_identities = [item.identity for item in plan.items
                           if item.classification == 'SAVED_ONLY']
        for index, identity in enumerate(cold_identities):
            if index:
                try:
                    latest = observe()
                    plan = restore.build_runtime_plan(
                        exact_layout, record, latest['inventory'], latest['unknown_pids'],
                        latest['process_failed'], host=socket.gethostname(), codex_home=home,
                        allow_unverified_live=args.allow_unverified_live, cwd_override=args.cwd,
                        planned_cwd=r.planned_cwd, context=latest['context'])
                except restore.RestoreError as exc:
                    failures.append(str(exc))
                    break
            item = next(now for now in plan.items if now.identity == identity)
            if item.classification == 'LIVE_MANAGED':
                continue
            if item.classification != 'SAVED_ONLY':
                failures.append('Runtime classification changed unexpectedly for ' + identity[2])
                break
            try:
                created, _ = r.ensure(item.row, ResumeFacade(), args.cwd, yolo=args.yolo)
                launched.append((item, created))
            except (RuntimeError, OSError) as exc:
                failures.append(item.identity[2] + ': ' + str(exc))
                break
        if failures:
            raise StateError('RUNTIME_PARTIAL: successful exact runtimes were retained; presentation was not started. ' +
                             ' | '.join(failures))
        fresh = observe()
        try:
            landed = restore.build_runtime_plan(
                exact_layout, record, fresh['inventory'], fresh['unknown_pids'],
                fresh['process_failed'], host=socket.gethostname(), codex_home=home,
                allow_unverified_live=args.allow_unverified_live, cwd_override=args.cwd,
                planned_cwd=r.planned_cwd, context=fresh['context'])
        except restore.RestoreError as exc:
            raise StateError('RUNTIME_PARTIAL: runtimes were kept, but full post-launch verification failed. ' +
                             str(exc)) from exc
        if any(item.classification != 'LIVE_MANAGED' for item in landed.items):
            raise StateError('RUNTIME_PARTIAL: not every exact conversation became a verified managed runtime.')
        try:
            for item, _ in launched:
                current = next(now for now in landed.items if now.identity == item.identity)
                b.store.annotate([current.row['managed']['_key'], thread_key(
                    item.identity[1], item.identity[2], item.identity[0])],
                    name=item.display_name, launch_cwd=item.cwd)
        except (RuntimeError, OSError) as exc:
            raise StateError('RUNTIME_PARTIAL: exact runtimes were retained, but local display metadata '
                             'could not be recorded; presentation was not started. ' + str(exc)) from exc

    if args.no_iterm:
        print(f'RUNTIME_RESOLVED: {len(landed.items)} exact conversations; presentation unchanged.')
        return {'state': 'RUNTIME_RESOLVED', 'runtimes': len(landed.items)}

    try:
        final_observation = observe()
        restore.revalidate_runtimes(landed.items, final_observation)
        cx_iterm.ensure_profile(b.store.preference('timestamps', True))
        result = restore.restore_live(
            landed.layout, landed.items, observe, final_observation['context'],
            cx_iterm.caller_tty())
        b.store.set_view_receipts(result.view_receipts)
    except restore.RestoreError as exc:
        raise StateError(str(exc) + ' All resolved Codex/zmx runtimes were retained.') from exc
    except (RuntimeError, OSError) as exc:
        raise StateError('PRESENTATION_PARTIAL: exact runtime generations remain healthy; ' + str(exc)) from exc
    print(f'{result.state}: exact workspace {b.clean(title)}\n'
          f'  existing views reused: {result.existing_views_reused}\n'
          f'  existing views moved: {result.existing_views_moved}\n'
          f'  existing views presentation-rebuilt: {result.existing_views_rebuilt}\n'
          f'  missing views created: {result.missing_views_created}\n'
          '  topology: exact\n'
          '  geometry: best effort')
    for limitation in result.geometry_limitations:
        print('  geometry limitation: ' + b.clean(limitation))
    if not args.no_dashboard:
        dashboard(b)
    return result


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
    capture = sub.add_parser('capture')
    capture.add_argument('name')
    capture.add_argument('--replace', action='store_true')
    op = sub.add_parser('open')
    op.add_argument('name')
    op.add_argument('--all', action='store_true', help='explicitly open all available members, without picker')
    op.add_argument('--list', action='store_true', help='preview only; no launches')
    op.add_argument('--no-iterm', action='store_true')
    op.add_argument('--no-dashboard', action='store_true')
    op.add_argument('--adaptive', action='store_true',
                    help='ignore optional exact layout and use the v0.7 adaptive view layout')
    op.add_argument('--allow-unverified-live', action='store_true',
                    help='accept unidentified-process risk only for saved-only cold resumes')
    op.add_argument('--cwd')
    op.add_argument('--per-tab', type=int)
    op.add_argument('--min-columns', type=int)
    op.add_argument('--min-rows', type=int)
    native().add_launch_policy_arguments(op)
    a = p.parse_args(argv)
    if a.action == 'save':
        return workspace_save(a.name, b, a.select, a.replace, layout(a))
    if a.action == 'capture':
        return workspace_capture(a.name, b, replace=a.replace)
    saved = b.store.read()['workspaces']
    if a.action == 'list':
        for title, record in saved.items():
            print(f"{b.clean(title)}  {len(record.get('members', []))} sessions  host={b.clean(record.get('host', '?'))}")
        return
    if a.name not in saved:
        raise StateError('No workspace with this exact name. Use cx workspace list.')
    if saved[a.name].get('exact_layout') is not None and not a.adaptive:
        return workspace_open_exact(a.name, saved[a.name], a, b)
    rows, history, missing = workspace_plan(saved[a.name], b, bind_threads=not a.list)
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
    chosen = rows if a.all else native().choose(rows, missing) if rows else []
    return launch_selected(chosen, history, a, b)


def text_lines(data):
    lines = [f"CX Deck {VERSION} | zmx | {data['host']} | {len(data['conversations'])} conversations"]
    for record in data['conversations']:
        display, runtime = record['display'], record['runtime']
        lines.append(f"{'*' if display['pinned'] else ' '} {display['name']} | "
                     f"conversation={record['conversation_state']} | runtime={record['runtime_state']} | "
                     f"view={record['view_state']}")
        if runtime.get('session'):
            lines.append('  ' + runtime['session'])
    lines.append('ALIVE = process exists, not task progress. NO_VIEW does not mean stopped.')
    lines.extend('WARNING: ' + str(w) for w in data.get('warnings', []))
    return [''.join(c if c.isprintable() else '?' for c in s) for s in lines]


def dashboard(b, interval=3, once=False, as_json=False):
    if not math.isfinite(interval) or interval < 1:
        raise StateError('Refresh interval must be finite and at least one second.')
    if once or as_json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        data = inventory_snapshot(b)
        print(json.dumps(cx_inventory.public(data), indent=2)
              if as_json else '\n'.join(text_lines(data)))
        return
    def screen(win):
        win.keypad(True)
        win.timeout(200)
        selected, cursor, query, editing = set(), None, '', False
        data, message, refresh_at = None, '', 0
        history_cache, history_warning, history_refresh_at = None, None, 0
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
            now = time.monotonic()
            if now >= refresh_at:
                try:
                    if history_cache is None or now >= history_refresh_at:
                        history_cache, _, history_warning = _read_history()
                        history_refresh_at = now + 30
                    data = inventory_snapshot(b, history=history_cache,
                                              history_warning=history_warning)
                except (RuntimeError, OSError) as exc:
                    message = 'Refresh failed; actions disabled: ' + str(exc)
                    data = None
                refresh_at = time.monotonic() + interval
            rows = list((data or {}).get('conversations', []))
            if query.strip():
                rows = cx_inventory.search(rows, query)
            keys = [r['key'] for r in rows]
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
                put(4 + i - offset,
                    f"{'[x]' if row['key'] in selected else '[ ]'} "
                    f"{'*' if row['display']['pinned'] else ' '} {row['display']['name']} | "
                    f"{row['conversation_state']} | {row['runtime_state']} | {row['view_state']}",
                    curses.A_REVERSE if row['key'] == cursor else 0)
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
                targets = [r for r in data['conversations'] if r['key'] in selected] if selected else [current]
                stale = selected - {r['key'] for r in data['conversations']}
                if stale and key in ('\n', '\r', 's'):
                    message = 'A selected session disappeared. Selection cleared; choose again.'
                    selected.clear()
                    continue
                if key in ('\n', '\r'):
                    managed = [r['_managed'] for r in targets if r.get('_managed') and
                               r['runtime_state'] in ('ALIVE', 'STOPPED')]
                    if len(managed) != len(targets):
                        message = 'Selected saved/external conversation has no managed view; use cx resume --select UUID.'
                        continue
                    message = external(lambda: focus_rows([
                        dict(row, display_name=record['display']['name'])
                        for row, record in zip(managed, targets)], b))
                elif key == 'r':
                    if not current.get('_managed'):
                        message = 'Rename from the dashboard currently requires a managed conversation.'
                        continue
                    message = external(lambda: annotate(current['_managed']['session'], b,
                                                         title=ask('New display name: ')))
                elif key == 'p':
                    if not current.get('_managed'):
                        message = 'Pin from the dashboard currently requires a managed conversation.'
                        continue
                    message = external(lambda: annotate(current['_managed']['session'], b,
                                                         pinned=not current['display']['pinned']))
                elif key == 's':
                    if any(not r.get('_managed') for r in targets):
                        message = 'Workspace save requires managed conversations; selection was not changed.'
                        continue
                    message = external(lambda: workspace_save(
                        ask(f'Save {len(targets)} selected session(s) as: '), b,
                        [r['_managed']['session'] for r in targets]))
                else:
                    continue
                refresh_at = 0
    try:
        return curses.wrapper(screen)
    except curses.error as exc:
        raise StateError('Cannot initialize interactive dashboard. Use cxl or cx status --json.') from exc
