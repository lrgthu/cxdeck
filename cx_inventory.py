"""Canonical read-only composition of CX Deck conversation/runtime/view facts.

This module joins bounded snapshots.  It never starts, attaches, labels, focuses,
or otherwise mutates Codex, zmx, iTerm, or the CX Deck Store.
"""
from __future__ import annotations

import copy
import math
import os
import socket

from cx_store import live_key, thread_key


SCHEMA = 'cxdeck.inventory/v1'
VIEW_STATES = frozenset({
    'VERIFIED_VIEW', 'NO_VIEW', 'UNVERIFIED_CLIENT', 'MULTIPLE_CLIENTS',
    'STALE_RECEIPT', 'CHANGED_GENERATION', 'VIEW_UNKNOWN', 'NOT_APPLICABLE',
})


class InventoryError(RuntimeError):
    pass


def _finite_timestamp(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _identity(host, home, tid):
    return (host, os.path.realpath(home), tid) if home and tid else None


def _workspace_index(state, host):
    by_identity, by_live = {}, {}
    for title, workspace in state.get('workspaces', {}).items():
        if not isinstance(workspace, dict) or workspace.get('host') != host:
            continue
        members = workspace.get('members', [])
        if not isinstance(title, str) or not isinstance(members, list):
            raise InventoryError('Invalid workspace metadata in the CX Deck Store.')
        for member in members:
            if not isinstance(member, dict):
                continue
            identity = _identity(host, member.get('home'), member.get('thread_id'))
            if identity:
                by_identity.setdefault(identity, set()).add(title)
            key = member.get('live_key')
            if isinstance(key, str) and key:
                by_live.setdefault(key, set()).add(title)
    return by_identity, by_live


def _prior_receipts(state, workspace_names, identity, current_key):
    result = []
    if not identity:
        return result
    for title in workspace_names:
        workspace = state.get('workspaces', {}).get(title, {})
        for member in workspace.get('members', []):
            if (_identity(workspace.get('host'), member.get('home'), member.get('thread_id')) == identity and
                    member.get('live_key') != current_key and member.get('live_key') in state.get('views', {})):
                result.append(copy.deepcopy(state['views'][member['live_key']]))
    return result


def classify_view(managed, context, client_ttys, iterm_views, receipt=None,
                  prior_receipts=(), provider_error=None, client_error=None):
    """Classify one generation from exact client tokens and one iTerm inventory."""
    if not managed:
        return {'state': 'NOT_APPLICABLE', 'verified': False, 'guid': None, 'tty': None,
                'client_ttys': [], 'attached_count': 0, 'receipt_state': 'NONE'}
    generation = managed.get('generation') or {}
    expected = (context.get('host'), os.path.realpath(context.get('runtime_dir') or ''),
                managed.get('session'), managed.get('daemon_pid'), managed.get('created'))
    observed = (generation.get('host'), os.path.realpath(generation.get('runtime_dir') or ''),
                generation.get('session'), generation.get('daemon_pid'), generation.get('created'))
    if observed != expected:
        return {'state': 'CHANGED_GENERATION', 'verified': False, 'guid': None, 'tty': None,
                'client_ttys': [], 'attached_count': managed.get('attached', 0),
                'receipt_state': 'UNKNOWN'}
    try:
        attached = int(managed.get('attached', 0))
    except (TypeError, ValueError):
        attached = -1
    if managed.get('state') == 'UNKNOWN' or client_error is not None or client_ttys is None:
        return {'state': 'VIEW_UNKNOWN', 'verified': False, 'guid': None, 'tty': None,
                'client_ttys': [], 'attached_count': attached,
                'receipt_state': 'UNKNOWN', 'detail': str(client_error or 'client provider unavailable')}
    if managed.get('state') not in ('ALIVE', 'STOPPED'):
        return {'state': 'NOT_APPLICABLE', 'verified': False, 'guid': None, 'tty': None,
                'client_ttys': [], 'attached_count': attached, 'receipt_state': 'NONE'}
    ttys = sorted(set(client_ttys))
    base = {'verified': False, 'guid': None, 'tty': None, 'client_ttys': ttys,
            'attached_count': attached, 'receipt_state': 'NONE'}
    if attached > 1 or len(ttys) > 1:
        return dict(base, state='MULTIPLE_CLIENTS')
    if not ttys:
        if attached > 0:
            return dict(base, state='UNVERIFIED_CLIENT')
        if prior_receipts:
            return dict(base, state='CHANGED_GENERATION', receipt_state='PRIOR_GENERATION')
        return dict(base, state='NO_VIEW', receipt_state='STALE' if receipt else 'NONE')
    if attached != 1:
        return dict(base, state='UNVERIFIED_CLIENT')
    if provider_error is not None or iterm_views is None:
        return dict(base, state='VIEW_UNKNOWN', receipt_state='UNKNOWN',
                    detail=str(provider_error or 'iTerm provider unavailable'))
    matches = [view for view in iterm_views if view.get('tty') == ttys[0]]
    if len(matches) != 1 or not matches[0].get('guid'):
        return dict(base, state='UNVERIFIED_CLIENT')
    actual = {'guid': matches[0]['guid'], 'tty': matches[0]['tty']}
    stale = bool(receipt and receipt != actual) or bool(not receipt and prior_receipts)
    return dict(base, state='STALE_RECEIPT' if stale else 'VERIFIED_VIEW',
                verified=True, guid=actual['guid'], tty=actual['tty'],
                receipt_state='STALE' if stale else ('CURRENT' if receipt else 'MISSING'))


def _conversation_state(saved, managed_live, external, bound):
    if not bound:
        return 'LIVE_ONLY_UNBOUND'
    if managed_live and external:
        return 'LIVE_MANAGED_EXTERNAL_CONFLICT'
    if saved and managed_live:
        return 'SAVED_AND_LIVE_MANAGED'
    if managed_live:
        return 'LIVE_MANAGED'
    if saved and external:
        return 'SAVED_AND_LIVE_EXTERNAL'
    if external:
        return 'LIVE_EXTERNAL'
    return 'SAVED'


def _external_runtime_state(pids, process_table):
    if not pids:
        return 'ABSENT'
    if process_table is None:
        return 'UNKNOWN'
    processes = [process_table.get(pid) for pid in pids]
    if any(not isinstance(process, dict) for process in processes):
        return 'UNKNOWN'
    return 'STOPPED' if all(str(process.get('stat', '')).startswith('T')
                            for process in processes) else 'ALIVE'


def compose(history_rows, rows, context, state, clients, *, iterm_views=None,
            view_error=None, client_error=None, unknown_pids=(), process_failed=False,
            warnings=(), timestamp=None, process_table=None):
    """Join one set of already-observed source snapshots into canonical records."""
    host = context.get('host') or socket.gethostname()
    saved_ids = {(os.path.realpath(row.get('home')), row.get('thread_id'))
                 for row in history_rows if row.get('home') and row.get('thread_id')}
    workspace_by_identity, workspace_by_live = _workspace_index(state, host)
    groups = state.get('groups', {})
    from cx_upgrade import runtime_compatibility
    from cx_version import VERSION
    records, managed_claims = [], {}
    for source in rows:
        item = copy.deepcopy(source)
        managed = copy.deepcopy(item.get('managed')) if item.get('managed') else None
        current_key = None
        if managed:
            managed['_key'] = current_key = live_key(managed, context)
        identity_tuple = _identity(host, item.get('home'), item.get('thread_id'))
        if identity_tuple and managed:
            managed_claims.setdefault(identity_tuple, []).append(managed)
        durable_key = thread_key(identity_tuple[1], identity_tuple[2], host) if identity_tuple else None
        metadata = {}
        if current_key:
            metadata.update(state.get('agents', {}).get(current_key, {}))
        if durable_key:
            metadata.update(state.get('agents', {}).get(durable_key, {}))
        group_id = metadata.get('group_id')
        group = groups.get(group_id) if group_id else None
        workspaces = set(workspace_by_identity.get(identity_tuple, set()))
        if current_key:
            workspaces.update(workspace_by_live.get(current_key, set()))
        workspaces = sorted(workspaces, key=str.casefold)
        external_pids = sorted(set(item.get('external_pids') or []))
        # A generation returned by zmx is a managed runtime fact.  Whether its
        # Codex process is ALIVE, STOPPED, NO_CODEX, or UNKNOWN is independent.
        managed_live = bool(managed)
        saved = bool(identity_tuple and (identity_tuple[1], identity_tuple[2]) in saved_ids)
        external_state = _external_runtime_state(external_pids, process_table)
        if managed:
            runtime_state = managed.get('state') or 'UNKNOWN'
            runtime_source = 'managed'
        elif external_pids:
            runtime_state = external_state
            runtime_source = 'external'
        else:
            runtime_state = 'ABSENT'
            runtime_source = 'none'
        recent_at = _finite_timestamp(item.get('updated')) if saved else None
        receipt = state.get('views', {}).get(current_key) if current_key else None
        old_receipts = _prior_receipts(state, workspaces, identity_tuple, current_key)
        view = classify_view(
            managed, context, clients.get(managed.get('sid')) if managed else [], iterm_views,
            receipt=receipt, prior_receipts=old_receipts, provider_error=view_error,
            client_error=client_error)
        title = (metadata.get('name') or item.get('title') or
                 (managed or {}).get('task') or item.get('thread_id') or (managed or {}).get('session'))
        runtime = {
            'managed': bool(managed), 'backend': 'zmx' if managed else None,
            'managed_state': managed.get('state') if managed else 'ABSENT',
            'session': managed.get('session') if managed else None,
            'generation': copy.deepcopy(managed.get('generation')) if managed else None,
            'codex_pids': list(managed.get('codex_pids') or []) if managed else [],
            'launch_policy': managed.get('launch_policy') if managed else None,
            'launch_mode': managed.get('launch_mode') if managed else None,
            'runtime_version': managed.get('runtime_version') if managed else None,
            'cx_version': ((managed.get('cx_version') or
                            (managed.get('labels') or {}).get('cx_version')) if managed else None),
            'live_key': current_key,
        }
        runtime['upgrade_state'] = (runtime_compatibility(runtime['cx_version'], VERSION)
                                    if managed else None)
        record = {
            'key': item.get('thread_id') or 'zmx:' + str((managed or {}).get('session', 'unknown')),
            'identity': ({'host': identity_tuple[0], 'codex_home': identity_tuple[1],
                          'thread_id': identity_tuple[2]} if identity_tuple else None),
            'conversation_state': _conversation_state(saved, managed_live, bool(external_pids), bool(identity_tuple)),
            'runtime_state': runtime_state,
            'runtime_source': runtime_source,
            'view_state': view['state'],
            'display': {'name': str(title), 'pinned': bool(metadata.get('pinned', False)),
                        'group_id': group_id if group else None,
                        'group': group.get('name') if group else 'Ungrouped',
                        'workspaces': workspaces},
            'history': {'saved': saved, 'updated_at': recent_at,
                        'cwd': item.get('cwd') if isinstance(item.get('cwd'), str) else None},
            'runtime': runtime,
            'external': {'pids': external_pids, 'state': external_state},
            'view': view,
            'recent_at': recent_at,
            'recent_source': 'codex_history_updated' if recent_at is not None else None,
            '_managed': managed,
        }
        records.append(record)
    duplicate = [identity for identity, claims in managed_claims.items() if len(claims) > 1]
    if duplicate:
        raise InventoryError('Two managed zmx generations claim the same conversation: ' +
                             ', '.join(identity[2] for identity in duplicate))
    records.sort(key=ordering_key)
    warnings = list(warnings)
    if client_error:
        warnings.append('zmx client verification unavailable: ' + str(client_error))
    if view_error:
        warnings.append('iTerm view inventory unavailable: ' + str(view_error))
    provider_errors = [str(error) for error in (client_error, view_error) if error]
    return {
        'schema': SCHEMA,
        'host': host,
        'timestamp': _finite_timestamp(timestamp),
        'conversations': records,
        'unidentified_pids': sorted(set(unknown_pids)),
        'process_inspection_failed': bool(process_failed),
        'view_provider': {
            'available': not provider_errors,
            'client_verification_available': client_error is None,
            'iterm_available': view_error is None,
            'error': '; '.join(provider_errors) if provider_errors else None,
        },
        'warnings': warnings,
        'context': {'backend': context.get('backend', 'zmx'), 'runtime_dir': context.get('runtime_dir'),
                    'runtime_version': context.get('runtime_version')},
    }


def ordering_key(record):
    display = record.get('display') or {}
    recent = record.get('recent_at')
    return (not bool(display.get('pinned')), -(recent if isinstance(recent, (int, float)) else -1),
            str(display.get('name') or '').casefold(), str(record.get('key') or ''))


def search(records, query):
    if not isinstance(query, str) or not query.strip() or any(not char.isprintable() for char in query):
        raise InventoryError('Search query must be printable and nonempty.')
    needle = query.strip().casefold()
    found = []
    for record in records:
        identity = record.get('identity') or {}
        tid = str(identity.get('thread_id') or '')
        display = record.get('display') or {}
        history = record.get('history') or {}
        exact = tid.casefold() == needle
        prefix = bool(tid and tid.casefold().startswith(needle))
        fields = [display.get('name'), display.get('group'), history.get('cwd')]
        if exact or prefix or any(needle in str(value).casefold() for value in fields if value):
            found.append((0 if exact else 1 if prefix else 2, record))
    return [record for _, record in sorted(found, key=lambda pair: (pair[0], ordering_key(pair[1])))]


def public(snapshot):
    """Return stable JSON without operation-only managed row references."""
    result = copy.deepcopy(snapshot)
    for record in result.get('conversations', []):
        for key in list(record):
            if key.startswith('_'):
                record.pop(key)
    return result
