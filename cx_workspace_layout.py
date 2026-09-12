"""Read-only exact iTerm workspace capture and layout schema validation.

The iTerm2 package is imported only by :func:`capture_live`.  Ordinary CX Deck
commands do not depend on it.  This module never creates, focuses, moves,
resizes, attaches, detaches, or writes to a terminal session.
"""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.metadata
import io
import json
import math
import os
import socket
import uuid


SCHEMA = 'cxdeck.workspace-layout/v1'
PROVIDER = 'iterm2-python'
FIDELITY = 'L3'
CAPTURE_ATTEMPTS = 3


class LayoutError(RuntimeError):
    pass


class LayoutChanged(LayoutError):
    pass


@dataclass(frozen=True)
class CaptureResult:
    layout: dict
    receipt: dict
    store_views: dict


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _plain_string(value, label):
    if (not isinstance(value, str) or not value or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise LayoutError(f'Invalid {label}.')
    return value


def _identity(value):
    if not isinstance(value, dict) or set(value) != {'host', 'codex_home', 'thread_id'}:
        raise LayoutError('Conversation identity must contain only host, codex_home, and thread_id.')
    host = _plain_string(value.get('host'), 'conversation host')
    home = _plain_string(value.get('codex_home'), 'conversation CODEX_HOME')
    if not os.path.isabs(home) or os.path.realpath(home) != home:
        raise LayoutError('Conversation CODEX_HOME must be an absolute real path.')
    try:
        tid = str(uuid.UUID(value.get('thread_id', '')))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LayoutError('Conversation thread_id must be an exact UUID.') from exc
    if tid != value['thread_id']:
        raise LayoutError('Conversation thread_id must use canonical UUID form.')
    return host, home, tid


def _finite_number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LayoutError(f'{label} must be finite.')
    if positive and value <= 0:
        raise LayoutError(f'{label} must be positive.')
    return value


def _frame_hint(value):
    if not isinstance(value, dict) or set(value) != {'x', 'y', 'width', 'height'}:
        raise LayoutError('frame_hint must contain x, y, width, and height.')
    x = _finite_number(value['x'], 'frame_hint x')
    y = _finite_number(value['y'], 'frame_hint y')
    width = _finite_number(value['width'], 'frame_hint width', positive=True)
    height = _finite_number(value['height'], 'frame_hint height', positive=True)
    if abs(x) > 10_000_000 or abs(y) > 10_000_000 or width > 1_000_000 or height > 1_000_000:
        raise LayoutError('frame_hint is outside sane presentation bounds.')
    return {'x': x, 'y': y, 'width': width, 'height': height}


def _validate_node(node, leaves, depth=0):
    if depth > 64 or not isinstance(node, dict):
        raise LayoutError('Invalid or excessively deep layout node.')
    node_type = node.get('type')
    if node_type == 'conversation':
        if set(node) != {'type', 'identity'}:
            raise LayoutError('Conversation leaves contain unsupported fields.')
        ident = _identity(node.get('identity'))
        if ident in leaves:
            raise LayoutError('Duplicate durable conversation leaf.')
        leaves.add(ident)
        return
    if node_type != 'split':
        raise LayoutError('Unknown workspace layout node type.')
    if not set(node).issubset({'type', 'axis', 'children', 'ratio_hints'}):
        raise LayoutError('Split node contains unsupported fields.')
    if node.get('axis') not in ('columns', 'rows'):
        raise LayoutError('Split axis must be columns or rows.')
    children = node.get('children')
    if not isinstance(children, list) or len(children) < 2:
        raise LayoutError('A durable split must contain at least two children.')
    ratios = node.get('ratio_hints')
    if ratios is not None:
        if not isinstance(ratios, list) or len(ratios) != len(children):
            raise LayoutError('ratio_hints must match the split child count.')
        values = [_finite_number(value, 'ratio hint') for value in ratios]
        if any(value < 0 for value in values) or sum(values) <= 0:
            raise LayoutError('ratio_hints must be nonnegative with a positive total.')
        if abs(sum(values) - 1.0) > 0.0001:
            raise LayoutError('ratio_hints must be normalized to one.')
    for child in children:
        _validate_node(child, leaves, depth + 1)


def validate_layout(layout):
    """Strictly validate and return a defensive copy of layout/v1."""
    if not isinstance(layout, dict) or set(layout) != {'schema', 'capture', 'windows'}:
        raise LayoutError('Workspace layout must contain only schema, capture, and windows.')
    if layout.get('schema') != SCHEMA:
        raise LayoutError('Unsupported workspace layout schema.')
    capture = layout.get('capture')
    if (not isinstance(capture, dict) or
            set(capture) != {'provider', 'provider_version', 'fidelity', 'captured_at'} or
            capture.get('provider') != PROVIDER or capture.get('fidelity') != FIDELITY):
        raise LayoutError('Invalid workspace capture metadata.')
    _plain_string(capture.get('provider_version'), 'capture provider version')
    timestamp = _plain_string(capture.get('captured_at'), 'capture timestamp')
    try:
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError as exc:
        raise LayoutError('capture timestamp must be ISO-8601.') from exc
    if parsed.tzinfo is None:
        raise LayoutError('capture timestamp must include a timezone.')
    windows = layout.get('windows')
    if not isinstance(windows, list) or not windows or len(windows) > 64:
        raise LayoutError('Workspace layout needs 1–64 windows.')
    leaves = set()
    tab_count = 0
    for window in windows:
        if not isinstance(window, dict) or not set(window).issubset({'frame_hint', 'tabs'}):
            raise LayoutError('Invalid workspace window record.')
        if 'frame_hint' in window:
            _frame_hint(window['frame_hint'])
        tabs = window.get('tabs')
        if not isinstance(tabs, list) or not tabs:
            raise LayoutError('Each workspace window needs at least one tab.')
        tab_count += len(tabs)
        if tab_count > 256:
            raise LayoutError('Workspace layout contains too many tabs.')
        for tab in tabs:
            if not isinstance(tab, dict) or set(tab) != {'root'}:
                raise LayoutError('Invalid workspace tab record.')
            _validate_node(tab.get('root'), leaves)
            if len(leaves) > 4096:
                raise LayoutError('Workspace layout contains too many conversations.')
    return copy.deepcopy(layout)


def conversation_identities(layout):
    validate_layout(layout)
    result = []
    def visit(node):
        if node['type'] == 'conversation':
            result.append(tuple(node['identity'][key] for key in ('host', 'codex_home', 'thread_id')))
            return
        for child in node['children']:
            visit(child)
    for window in layout['windows']:
        for tab in window['tabs']:
            visit(tab['root'])
    return result


def _workspace_identities(workspace):
    if not isinstance(workspace, dict):
        raise LayoutError('Invalid workspace record.')
    host = _plain_string(workspace.get('host'), 'workspace host')
    members = workspace.get('members')
    if not isinstance(members, list) or not members:
        raise LayoutError('Workspace has no members to capture.')
    result = []
    for member in members:
        if not isinstance(member, dict):
            raise LayoutError('Invalid workspace member.')
        if not member.get('thread_id'):
            raise LayoutError('Workspace contains a live-only member with no exact UUID; exact capture is unavailable.')
        result.append(_identity(dict(host=host, codex_home=member.get('home'),
                                     thread_id=member.get('thread_id'))))
    if len(set(result)) != len(result):
        raise LayoutError('Workspace membership contains a duplicate conversation identity.')
    return result


def _generation(row, context):
    if not isinstance(row, dict) or row.get('backend') != 'zmx':
        raise LayoutError('Workspace capture supports verified managed zmx sessions only.')
    generation = row.get('generation') if isinstance(row, dict) else None
    required = ('host', 'runtime_dir', 'session', 'daemon_pid', 'created')
    if not isinstance(generation, dict) or any(key not in generation for key in required):
        raise LayoutError('Managed session has incomplete zmx generation identity.')
    host = _plain_string(generation.get('host'), 'zmx generation host')
    runtime_dir = generation['runtime_dir']
    session = generation['session']
    if not isinstance(runtime_dir, str) or not os.path.isabs(runtime_dir) or os.path.realpath(runtime_dir) != runtime_dir:
        raise LayoutError('Managed session has invalid zmx runtime directory.')
    _plain_string(session, 'zmx session name')
    try:
        daemon_pid, created = int(generation['daemon_pid']), int(generation['created'])
    except (TypeError, ValueError) as exc:
        raise LayoutError('Managed session has invalid zmx generation values.') from exc
    if daemon_pid <= 0 or created <= 0:
        raise LayoutError('Managed session has invalid zmx generation values.')
    if (host != context.get('host') or runtime_dir != context.get('runtime_dir') or
            session != row.get('session') or daemon_pid != row.get('daemon_pid') or
            created != row.get('created')):
        raise LayoutError('Managed session generation disagrees with its current zmx observation.')
    return (host, runtime_dir, session, daemon_pid, created)


def _runtime_identity(row):
    home, tid, host = row.get('codex_home'), row.get('thread_id'), (row.get('generation') or {}).get('host')
    if not home or not tid or not host:
        return None
    return _identity(dict(host=host, codex_home=home, thread_id=tid))


def _raw_leaves(node):
    if not isinstance(node, dict):
        raise LayoutError('iTerm returned an invalid topology node.')
    if node.get('type') == 'session':
        yield node
        return
    if node.get('type') != 'split' or node.get('axis') not in ('columns', 'rows'):
        raise LayoutError('iTerm returned an unsupported topology node.')
    children = node.get('children')
    if not isinstance(children, list) or not children:
        raise LayoutError('iTerm returned an empty split node.')
    for child in children:
        yield from _raw_leaves(child)


def _raw_span(node):
    if node['type'] == 'session':
        grid = node.get('grid')
        if not isinstance(grid, dict):
            return None
        columns, rows = grid.get('columns'), grid.get('rows')
        if (isinstance(columns, bool) or isinstance(rows, bool) or
                not isinstance(columns, (int, float)) or not isinstance(rows, (int, float)) or
                not math.isfinite(columns) or not math.isfinite(rows) or columns <= 0 or rows <= 0):
            return None
        return float(columns), float(rows)
    spans = [_raw_span(child) for child in node['children']]
    if any(span is None for span in spans):
        return None
    if node['axis'] == 'columns':
        return sum(span[0] for span in spans), max(span[1] for span in spans)
    return max(span[0] for span in spans), sum(span[1] for span in spans)


def _ratio_hints(node):
    index = 0 if node['axis'] == 'columns' else 1
    spans = [_raw_span(child) for child in node['children']]
    if any(span is None for span in spans):
        return None
    sizes = [span[index] for span in spans]
    total = sum(sizes)
    if not math.isfinite(total) or total <= 0:
        return None
    ratios = [round(value / total, 8) for value in sizes]
    # Make the serialized sum exactly one despite decimal rounding.
    ratios[-1] = round(1.0 - sum(ratios[:-1]), 8)
    if any(not math.isfinite(value) or value < 0 for value in ratios):
        return None
    return ratios


def _optional_frame(value):
    try:
        return _frame_hint(value)
    except LayoutError:
        return None


def _validate_raw_snapshot(raw):
    if not isinstance(raw, dict) or raw.get('provider') != PROVIDER:
        raise LayoutError('Unrecognized iTerm topology provider response.')
    _plain_string(raw.get('provider_version'), 'iTerm topology provider version')
    windows = raw.get('windows')
    if not isinstance(windows, list):
        raise LayoutError('iTerm topology response has no window list.')
    window_ids, tab_ids, guids, ttys = set(), set(), set(), set()
    for window in windows:
        if not isinstance(window, dict):
            raise LayoutError('iTerm returned an invalid window record.')
        window_id = _plain_string(window.get('window_id'), 'iTerm window ID')
        if window_id in window_ids:
            raise LayoutError('iTerm returned a duplicate window ID.')
        window_ids.add(window_id)
        tabs = window.get('tabs')
        if not isinstance(tabs, list) or not tabs:
            raise LayoutError('iTerm returned an empty window.')
        for tab in tabs:
            tab_id = _plain_string(tab.get('tab_id'), 'iTerm tab ID')
            if tab_id in tab_ids:
                raise LayoutError('iTerm returned a duplicate tab ID.')
            tab_ids.add(tab_id)
            minimized = tab.get('minimized_session_ids')
            if not isinstance(minimized, list) or any(not isinstance(item, str) for item in minimized):
                raise LayoutError('iTerm returned invalid minimized-session metadata.')
            for leaf in _raw_leaves(tab.get('root')):
                guid = _plain_string(leaf.get('guid'), 'iTerm session GUID')
                tty = _plain_string(leaf.get('tty'), 'iTerm session TTY')
                if not tty.startswith('/dev/'):
                    raise LayoutError('iTerm session has an invalid TTY.')
                if guid in guids or tty in ttys:
                    raise LayoutError('iTerm returned duplicate session GUID/TTY metadata.')
                guids.add(guid)
                ttys.add(tty)


def _bind_snapshot(workspace, raw, runtime):
    targets = _workspace_identities(workspace)
    _validate_raw_snapshot(raw)
    if not isinstance(runtime, dict) or not isinstance(runtime.get('sessions'), list):
        raise LayoutError('Invalid managed zmx runtime observation.')
    context = runtime.get('context')
    if (not isinstance(context, dict) or context.get('backend') != 'zmx' or
            not isinstance(context.get('host'), str) or
            not isinstance(context.get('runtime_dir'), str) or
            not os.path.isabs(context['runtime_dir']) or
            os.path.realpath(context['runtime_dir']) != context['runtime_dir']):
        raise LayoutError('Invalid zmx runtime context.')
    clients = runtime.get('clients')
    views = runtime.get('views')
    if not isinstance(clients, dict) or not isinstance(views, dict):
        raise LayoutError('Incomplete zmx client or cached-view observation.')

    claims = {}
    rows_by_identity = {}
    all_tty_rows = {}
    for row in runtime['sessions']:
        ident = _runtime_identity(row)
        live = row.get('state') in ('ALIVE', 'STOPPED')
        if ident is not None and live:
            claims.setdefault(ident, []).append(row)
        sid = row.get('sid')
        row_clients = clients.get(sid, [])
        if (ident is not None and live and isinstance(row_clients, (list, tuple, set)) and
                len(row_clients) == 1 and row.get('attached') == 1):
            tty = next(iter(row_clients))
            if isinstance(tty, str):
                all_tty_rows.setdefault(tty, []).append((ident, row))
    duplicates = [ident for ident, rows in claims.items() if len(rows) > 1]
    if duplicates:
        raise LayoutError('Two managed zmx generations claim the same durable conversation; capture refused.')
    rows_by_identity.update({ident: rows[0] for ident, rows in claims.items()})

    selected_by_tty = {}
    selected_generations = set()
    selected_view_receipts = {}
    for ident in targets:
        row = rows_by_identity.get(ident)
        if row is None:
            raise LayoutError('Workspace member is not a verified live managed zmx conversation: ' + ident[2])
        if row.get('state') not in ('ALIVE', 'STOPPED'):
            raise LayoutError(f"Workspace member {ident[2]} is not live ({row.get('state', 'UNKNOWN')}).")
        generation = _generation(row, context)
        if generation in selected_generations:
            raise LayoutError('Two workspace members unexpectedly claim one zmx generation.')
        selected_generations.add(generation)
        row_clients = clients.get(row.get('sid'), [])
        if not isinstance(row_clients, (list, tuple, set)):
            raise LayoutError('Invalid zmx client observation.')
        client_set = set(row_clients)
        if row.get('attached', 0) > 1 or len(client_set) > 1:
            raise LayoutError(f'Multiple zmx clients match workspace member {ident[2]}; capture is ambiguous.')
        if row.get('attached') != 1 or len(client_set) != 1:
            raise LayoutError(f'No verified zmx client matches workspace member {ident[2]}.')
        live_key = row.get('_key')
        if not isinstance(live_key, str) or not live_key:
            raise LayoutError('Managed session is missing its exact CX Deck live-generation key.')
        tty = next(iter(client_set))
        if not isinstance(tty, str) or not tty.startswith('/dev/') or tty in selected_by_tty:
            raise LayoutError('A zmx client TTY is missing or ambiguous.')
        selected_by_tty[tty] = dict(identity=ident, row=row, generation=generation)
        selected_view_receipts[live_key] = copy.deepcopy(views.get(live_key))

    selected_window_ids = []
    for window in raw['windows']:
        if any(leaf['tty'] in selected_by_tty for tab in window['tabs']
               for leaf in _raw_leaves(tab['root'])):
            selected_window_ids.append(window['window_id'])
    if not selected_window_ids:
        raise LayoutError('No selected workspace conversation has a verified iTerm view.')

    consumed, durable_windows, receipt_windows = set(), [], []
    for window in raw['windows']:
        if window['window_id'] not in selected_window_ids:
            continue
        durable_tabs, receipt_tabs = [], []
        for tab in window['tabs']:
            if tab['minimized_session_ids']:
                raise LayoutError('Exact workspace capture does not yet support minimized/maximized panes. Restore the normal split view and retry.')

            def bind_node(node, root=False):
                if node['type'] == 'session':
                    tty, guid = node['tty'], node['guid']
                    binding = selected_by_tty.get(tty)
                    if binding is None:
                        other = all_tty_rows.get(tty, [])
                        if len(other) == 1:
                            raise LayoutError('Unexpected managed conversation shares a selected iTerm window: ' + other[0][0][2])
                        raise LayoutError(f'Unverified iTerm pane in selected window: GUID={guid} TTY={tty}. Mixed managed/unmanaged windows are unsupported.')
                    ident, row, generation = binding['identity'], binding['row'], binding['generation']
                    if ident in consumed:
                        raise LayoutError('One conversation appears in more than one iTerm leaf.')
                    consumed.add(ident)
                    cached = views.get(row.get('_key'))
                    if cached is not None:
                        if (not isinstance(cached, dict) or
                                cached.get('guid') != guid or cached.get('tty') != tty):
                            raise LayoutError('Cached iTerm view receipt disagrees with the verified live GUID/TTY.')
                    durable = {'type': 'conversation', 'identity': {
                        'host': ident[0], 'codex_home': ident[1], 'thread_id': ident[2]}}
                    receipt = {'type': 'session', 'guid': guid, 'tty': tty,
                               'identity': list(ident), 'generation': list(generation),
                               'grid': node.get('grid'), 'frame': node.get('frame')}
                    return durable, receipt
                children = node.get('children')
                if len(children) == 1:
                    if not root:
                        raise LayoutError('iTerm returned an unsupported nested one-child splitter.')
                    return bind_node(children[0])
                if len(children) < 2:
                    raise LayoutError('iTerm returned an invalid split tree.')
                bound = [bind_node(child) for child in children]
                durable = {'type': 'split', 'axis': node['axis'],
                           'children': [item[0] for item in bound]}
                ratios = _ratio_hints(node)
                if ratios is not None:
                    durable['ratio_hints'] = ratios
                receipt = {'type': 'split', 'axis': node['axis'],
                           'children': [item[1] for item in bound]}
                return durable, receipt

            durable_root, receipt_root = bind_node(tab['root'], root=True)
            durable_tabs.append({'root': durable_root})
            receipt_tabs.append({'tab_id': tab['tab_id'], 'root': receipt_root,
                                 'minimized_session_ids': list(tab['minimized_session_ids'])})
        durable_window = {'tabs': durable_tabs}
        frame = _optional_frame(window.get('frame'))
        if frame is not None:
            durable_window['frame_hint'] = frame
        durable_windows.append(durable_window)
        receipt_windows.append({'window_id': window['window_id'], 'frame': window.get('frame'),
                                'tabs': receipt_tabs})

    target_set = set(targets)
    if consumed != target_set:
        missing = sorted(ident[2] for ident in target_set - consumed)
        unexpected = sorted(ident[2] for ident in consumed - target_set)
        raise LayoutError('Workspace membership mismatch. Missing from iTerm: ' +
                          (', '.join(missing) or 'none') + '; unexpected: ' +
                          (', '.join(unexpected) or 'none') + '.')
    content = {'windows': durable_windows}
    receipt = {'provider': raw['provider'], 'provider_version': raw['provider_version'],
               'windows': receipt_windows,
               'cached_views': selected_view_receipts,
               'selected_clients': {tty: {'identity': list(value['identity']),
                                           'generation': list(value['generation'])}
                                    for tty, value in sorted(selected_by_tty.items())}}
    return content, receipt


async def capture_contract(workspace, provider, runtime_reader, attempts=CAPTURE_ATTEMPTS, clock=None):
    """Capture two matching read-only observations and return layout plus receipt."""
    if not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise LayoutError('Capture retry count must be between one and five.')
    clock = clock or (lambda: datetime.now(timezone.utc))
    last = None
    for attempt in range(attempts):
        raw_a = await provider.read()
        runtime_a = runtime_reader()
        content_a, receipt_a = _bind_snapshot(workspace, raw_a, runtime_a)
        try:
            raw_b = await provider.read()
            runtime_b = runtime_reader()
            content_b, receipt_b = _bind_snapshot(workspace, raw_b, runtime_b)
        except LayoutError as exc:
            last = exc
            if attempt + 1 == attempts:
                break
            continue
        if (_canonical(content_a) == _canonical(content_b) and
                _canonical(receipt_a) == _canonical(receipt_b)):
            moment = clock()
            if not isinstance(moment, datetime) or moment.tzinfo is None:
                raise LayoutError('Capture clock returned an invalid timestamp.')
            captured_at = moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            layout = {'schema': SCHEMA,
                      'capture': {'provider': PROVIDER,
                                  'provider_version': raw_b['provider_version'],
                                  'fidelity': FIDELITY,
                                  'captured_at': captured_at},
                      'windows': content_b['windows']}
            validate_layout(layout)
            return CaptureResult(layout, receipt_b, copy.deepcopy(runtime_b['views']))
        last = LayoutChanged('iTerm topology, geometry, client, or generation changed between reads.')
    detail = f' Last observation: {last}' if last else ''
    raise LayoutChanged(f'Workspace layout did not stabilize after {attempts} complete capture attempts.{detail}')


class ITermPythonReader:
    """Read-only adapter over supported iTerm2 Python API objects."""
    def __init__(self, iterm2_module, app, provider_version):
        self.iterm2 = iterm2_module
        self.app = app
        self.provider_version = provider_version

    @staticmethod
    def _frame(frame):
        return {'x': frame.origin.x, 'y': frame.origin.y,
                'width': frame.size.width, 'height': frame.size.height}

    async def _node(self, node):
        if isinstance(node, self.iterm2.Session):
            grid = node.grid_size
            return {'type': 'session', 'guid': node.session_id,
                    'tty': await node.async_get_variable('tty'),
                    'grid': {'columns': grid.width, 'rows': grid.height},
                    'frame': self._frame(node.frame)}
        if not isinstance(node, self.iterm2.Splitter):
            raise LayoutError('iTerm returned an unsupported split-tree object.')
        return {'type': 'split', 'axis': 'columns' if node.vertical else 'rows',
                'children': [await self._node(child) for child in node.children]}

    async def read(self):
        await self.app.async_refresh()
        windows = []
        for window in list(self.app.windows):
            frame = await window.async_get_frame()
            tabs = []
            for tab in list(window.tabs):
                tabs.append({'tab_id': tab.tab_id,
                             'minimized_session_ids': [item.session_id for item in tab.minimized_sessions],
                             'root': await self._node(tab.root)})
            if tabs:
                windows.append({'window_id': window.window_id, 'frame': self._frame(frame),
                                'tabs': tabs})
        return {'provider': PROVIDER, 'provider_version': self.provider_version,
                'windows': windows}


def capture_live(workspace, runtime_reader, attempts=CAPTURE_ATTEMPTS):
    """Lazy feature boundary for real read-only iTerm capture."""
    try:
        import iterm2  # type: ignore
    except ImportError as exc:
        raise LayoutError(
            "Exact workspace capture requires the optional iTerm2 Python package. "
            "Install it explicitly with: python3 -m pip install --user 'iterm2==2.23'") from exc
    if socket.gethostname() != workspace.get('host'):
        raise LayoutError('Workspace belongs to another host; exact iTerm capture is local only.')
    try:
        provider_version = importlib.metadata.version('iterm2')
    except importlib.metadata.PackageNotFoundError as exc:
        raise LayoutError('Cannot determine the installed iTerm2 Python package version.') from exc
    holder = {}
    async def main(connection):
        app = await iterm2.async_get_app(connection)
        if app is None:
            raise LayoutError('iTerm2 application hierarchy is unavailable.')
        holder['result'] = await capture_contract(
            workspace, ITermPythonReader(iterm2, app, provider_version),
            runtime_reader, attempts=attempts)
    incidental = io.StringIO()
    try:
        with contextlib.redirect_stdout(incidental):
            iterm2.run_until_complete(main)
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError('iTerm2 Python capture failed without changing any runtime: ' + str(exc)) from exc
    return holder['result']
