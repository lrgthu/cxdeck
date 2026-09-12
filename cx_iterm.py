"""Native iTerm2 views of verified zmx generations."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import stat

from cx_store import StateError, live_key, name

PROFILE_NAME = 'CX Deck'
PROFILE_GUID = '9E7CB78D-0DB7-4AC4-B883-3A99289F43D4'
PROFILE_FILE = 'CX Deck.json'

APPLESCRIPT = r'''on run argv
    set operation to item 1 of argv
    tell application id "com.googlecode.iterm2"
        if operation is "preflight" then return version
        if operation is "inventory" then
            set output to ""
            set ws to get windows
            repeat with wi from 1 to count of ws
                set w to item wi of ws
                set ts to get tabs of w
                repeat with ti from 1 to count of ts
                    set t to item ti of ts
                    set ss to get sessions of t
                    repeat with si from 1 to count of ss
                        set s to item si of ss
                        set output to output & (unique id of s) & (ASCII character 9) & (tty of s) & linefeed
                    end repeat
                end repeat
            end repeat
            return output
        end if
        if operation is "focus" then
            set ws to get windows
            repeat with wi from 1 to count of ws
                set w to item wi of ws
                set ts to get tabs of w
                repeat with ti from 1 to count of ts
                    set t to item ti of ts
                    set ss to get sessions of t
                    repeat with si from 1 to count of ss
                        set s to item si of ss
                        if (unique id of s) is (item 2 of argv) and (tty of s) is (item 3 of argv) then
                            tell w to select
                            tell t to select
                            tell s to select
                            activate
                            return "focused"
                        end if
                    end repeat
                end repeat
            end repeat
            error "The selected iTerm2 pane disappeared; no new view created."
        end if
        if operation is "present" then
            set ws to get windows
            repeat with wi from 1 to count of ws
                set w to item wi of ws
                set ts to get tabs of w
                repeat with ti from 1 to count of ts
                    set t to item ti of ts
                    set ss to get sessions of t
                    repeat with si from 1 to count of ss
                        set s to item si of ss
                        if (unique id of s) is (item 2 of argv) and (tty of s) is (item 3 of argv) then
                            set name of s to (item 4 of argv)
                            set variable s named "user.cxdeck_name" to (item 4 of argv)
                            return "presented"
                        end if
                    end repeat
                end repeat
            end repeat
            error "The selected iTerm2 pane disappeared; presentation was not changed."
        end if
        if operation is "inspect" then
            set ws to get windows
            repeat with wi from 1 to count of ws
                set w to item wi of ws
                set ts to get tabs of w
                repeat with ti from 1 to count of ts
                    set t to item ti of ts
                    set ss to get sessions of t
                    repeat with si from 1 to count of ss
                        set s to item si of ss
                        if (unique id of s) is (item 2 of argv) and (tty of s) is (item 3 of argv) then
                            set badgeName to ""
                            try
                                set badgeName to variable s named "user.cxdeck_name"
                            end try
                            return (name of s) & (ASCII character 9) & badgeName & (ASCII character 9) & (profile name of s) & (ASCII character 9) & ((id of w) as text)
                        end if
                    end repeat
                end repeat
            end repeat
            error "The selected iTerm2 pane disappeared; presentation was not inspected."
        end if
        if operation is not "open" then error "Unknown operation"
        set mode to item 2 of argv
        set anchorTTY to item 3 of argv
        set minColumns to (item 4 of argv) as integer
        set minRows to (item 5 of argv) as integer
        set perTab to (item 6 of argv) as integer
        set cxProfile to item 7 of argv

        -- Materialize every application collection before indexed traversal.
        -- Keep only scalar IDs between traversals and never mutate a collection
        -- while walking it.
        set anchorGuid to ""
        set targetWindowID to ""
        set paneGuids to {}
        set ws to get windows
        repeat with wi from 1 to count of ws
            set w to item wi of ws
            set ts to get tabs of w
            repeat with ti from 1 to count of ts
                set t to item ti of ts
                set ss to get sessions of t
                repeat with si from 1 to count of ss
                    set s to item si of ss
                    if (tty of s) is anchorTTY then
                        set anchorGuid to (unique id of s) as text
                        set targetWindowID to (id of w) as text
                    end if
                end repeat
            end repeat
        end repeat
        if mode is "split" and anchorGuid is "" then error "Invoking pane not found"
        if mode is "split" then set paneGuids to {anchorGuid}
        if mode is "window" then set targetWindowID to ""

        set output to ""
        set paneCount to ((count of argv) - 7) div 2
        repeat with idx from 1 to paneCount
            set paneCommand to item (6 + idx * 2) of argv
            set paneName to item (7 + idx * 2) of argv
            set bestGuid to ""
            set bestArea to 0
            set bestDirection to "h"

            -- Re-resolve each candidate by stable session GUID. paneGuids is a
            -- plain list of strings, never a list of iTerm application objects.
            if mode is not "tab" and (perTab is 0 or (count of paneGuids) < perTab) then
                repeat with paneGuidRef in paneGuids
                    set paneGuid to paneGuidRef as text
                    set ws to get windows
                    repeat with wi from 1 to count of ws
                        set w to item wi of ws
                        set ts to get tabs of w
                        repeat with ti from 1 to count of ts
                            set t to item ti of ts
                            set ss to get sessions of t
                            repeat with si from 1 to count of ss
                                set s to item si of ss
                                if ((unique id of s) as text) is paneGuid then
                                    set cols to columns of s
                                    set rs to rows of s
                                    set canV to cols >= (2 * minColumns + 1)
                                    set canH to rs >= (2 * minRows + 1)
                                    if (canV or canH) and cols * rs > bestArea then
                                        set bestArea to cols * rs
                                        set bestGuid to paneGuid
                                        if canV and ((not canH) or cols / minColumns >= rs / minRows) then
                                            set bestDirection to "v"
                                        else
                                            set bestDirection to "h"
                                        end if
                                    end if
                                end if
                            end repeat
                        end repeat
                    end repeat
                end repeat
            end if

            set childGuid to ""
            set childTTY to ""
            if targetWindowID is "" then
                set newWindow to (create window with profile cxProfile command paneCommand)
                set targetWindowID to (id of newWindow) as text
                set childPane to current session of current tab of newWindow
                if paneCount > 1 then
                    try
                        set columns of childPane to 180
                        set rows of childPane to 56
                    end try
                end if
                set childGuid to (unique id of childPane) as text
                set childTTY to (tty of childPane) as text
                set paneGuids to {childGuid}
            else if bestGuid is "" then
                set targetWindow to missing value
                set ws to get windows
                repeat with wi from 1 to count of ws
                    set w to item wi of ws
                    if ((id of w) as text) is targetWindowID then
                        set targetWindow to w
                        exit repeat
                    end if
                end repeat
                if targetWindow is missing value then error "Target iTerm2 window disappeared"
                tell targetWindow
                    set nextTab to (create tab with profile cxProfile command paneCommand)
                    set childPane to current session of nextTab
                end tell
                set childGuid to (unique id of childPane) as text
                set childTTY to (tty of childPane) as text
                set paneGuids to {childGuid}
            else
                set splitPane to missing value
                set ws to get windows
                repeat with wi from 1 to count of ws
                    set w to item wi of ws
                    set ts to get tabs of w
                    repeat with ti from 1 to count of ts
                        set t to item ti of ts
                        set ss to get sessions of t
                        repeat with si from 1 to count of ss
                            set s to item si of ss
                            if ((unique id of s) as text) is bestGuid then
                                set splitPane to s
                                exit repeat
                            end if
                        end repeat
                        if splitPane is not missing value then exit repeat
                    end repeat
                    if splitPane is not missing value then exit repeat
                end repeat
                if splitPane is missing value then error "Selected iTerm2 pane disappeared during layout"
                tell splitPane
                    if bestDirection is "v" then
                        set childPane to (split vertically with profile cxProfile command paneCommand)
                    else
                        set childPane to (split horizontally with profile cxProfile command paneCommand)
                    end if
                end tell
                set childGuid to (unique id of childPane) as text
                set childTTY to (tty of childPane) as text
                set end of paneGuids to childGuid
            end if
            if (profile name of childPane) is not cxProfile then error "iTerm2 did not create the pane with the CX Deck profile"
            set name of childPane to paneName
            set variable childPane named "user.cxdeck_name" to paneName
            set output to output & childGuid & (ASCII character 9) & childTTY & linefeed
        end repeat
        activate
        return output
    end tell
end run
'''


def caller_tty():
    try:
        return os.ttyname(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return ''


def _view_parts(line):
    """Parse one iTerm inventory row without guessing pane identity.

    Current bridge emits an ASCII HT delimiter. v0.5 candidates before
    2026-09-09 accidentally emitted the literal word ``tab`` on real iTerm2;
    accept that exact compatibility shape only when the left side is a UUID and the
    right side is a local /dev tty. This keeps already-cloned candidates usable
    after a pull while remaining fail-closed for arbitrary strings.
    """
    if '\t' in line:
        parts = line.split('\t')
        if len(parts) == 2:
            return parts
        return None
    match = re.fullmatch(r'([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})tab(/dev/[^\s]+)', line)
    return list(match.groups()) if match else None


def parse_views(text):
    views, seen = [], {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = _view_parts(line)
        if not parts or not parts[0] or not parts[1].startswith('/dev/'):
            raise StateError('Unrecognized iTerm2 view response; no pane identity guessed.')
        previous = seen.get(parts[0])
        if previous is not None:
            if previous != parts[1]:
                raise StateError('Conflicting iTerm2 pane identities.')
            continue
        seen[parts[0]] = parts[1]
        views.append(dict(guid=parts[0], tty=parts[1]))
    return views


def profile_payload(timestamps=True):
    """Return the minimal iTerm dynamic profile owned by CX Deck.

    iTerm merges omitted keys from the current default profile. These overrides
    affect only sessions explicitly created with the CX Deck profile.
    """
    return {
        'Profiles': [{
            'Name': PROFILE_NAME,
            'Guid': PROFILE_GUID,
            'Badge Text': r'\(user.cxdeck_name)',
            'Timestamps Visible': bool(timestamps),
            'Timestamps Style': 1,
        }]
    }


def _profile_path(home=None):
    return Path(home or Path.home()) / 'Library/Application Support/iTerm2/DynamicProfiles' / PROFILE_FILE


def _validate_profile_parents(path):
    for parent in (path.parents[3], path.parents[2], path.parents[1], path.parents[0]):
        if parent.is_symlink():
            raise StateError(f'iTerm2 profile path is a symlink: {parent}')
        if parent.exists():
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise StateError(f'iTerm2 profile path is not an owned directory: {parent}')


def ensure_profile(timestamps=True, home=None):
    """Install/update only CX Deck's dynamic iTerm profile, atomically."""
    path = _profile_path(home)
    _validate_profile_parents(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise StateError('CX Deck iTerm2 profile is a symlink; refusing to replace it.')
    if path.exists():
        if path.stat().st_uid != os.getuid() or not stat.S_ISREG(path.stat().st_mode):
            raise StateError('CX Deck iTerm2 profile is not an owned regular file.')
        try:
            current = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError) as exc:
            raise StateError('Existing CX Deck iTerm2 profile is invalid; refusing to overwrite it.') from exc
        profiles = current.get('Profiles') if isinstance(current, dict) else None
        if (not isinstance(profiles, list) or len(profiles) != 1 or
                profiles[0].get('Guid') != PROFILE_GUID):
            raise StateError('Existing CX Deck iTerm2 profile is not owned by this installation.')
    desired = (json.dumps(profile_payload(timestamps), indent=2, ensure_ascii=True) + '\n').encode()
    if (path.exists() and path.read_bytes() == desired and
            stat.S_IMODE(path.stat().st_mode) == 0o600):
        return path, False
    fd, temporary = tempfile.mkstemp(prefix='.cxdeck-profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path, True


def validate_profile_removal(home=None):
    """Confirm a present profile is an owned regular file without mutating it."""
    path = _profile_path(home)
    _validate_profile_parents(path)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise StateError('CX Deck iTerm2 profile is a symlink; refusing to remove it.')
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
        raise StateError('The CX Deck iTerm2 profile is not an owned regular file.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        profiles = data.get('Profiles') if isinstance(data, dict) else None
        owned = isinstance(profiles, list) and len(profiles) == 1 and profiles[0].get('Guid') == PROFILE_GUID
    except (OSError, ValueError, UnicodeError):
        owned = False
    if not owned:
        raise StateError('The CX Deck iTerm2 profile file is not owned by this installation.')
    return path


def remove_profile(home=None):
    """Remove only the profile file carrying CX Deck's fixed ownership GUID."""
    path = validate_profile_removal(home)
    if path is None:
        return False
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def prepare_current_view(display_name, timestamps=True, stream=None, environ=None,
                         platform=None, home=None):
    """Apply the CX Deck profile to the current iTerm session before attach.

    This uses iTerm's supported OSC controls on the caller's terminal. It writes
    no printable text and never enters the Codex/zmx PTY.
    """
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform != 'darwin' or environ.get('TERM_PROGRAM') != 'iTerm.app' or not stream.isatty():
        return False
    display_name = name(display_name)
    ensure_profile(timestamps, home)
    encoded = base64.b64encode(display_name.encode('utf-8')).decode('ascii')
    stream.write(f'\x1b]1337;SetProfile={PROFILE_NAME}\x1b\\'
                 f'\x1b]1337;SetUserVar=cxdeck_name={encoded}\x1b\\'
                 f'\x1b]1;{display_name}\x1b\\')
    stream.flush()
    return True


class ITerm:
    def configure(self, timestamps=True):
        return ensure_profile(timestamps)

    def call(self, *args):
        if sys.platform != 'darwin' or not shutil.which('osascript'):
            raise StateError('GUI operations require local macOS/iTerm2. Use cx or cx resume --no-iterm.')
        try:
            result = subprocess.run(['osascript', '-', *map(str, args)], input=APPLESCRIPT,
                text=True, capture_output=True, timeout=max(30, len(args) * 5))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateError(f"iTerm2 {args[0] if args else 'operation'} unconfirmed. Open iTerm2 and resolve any first-launch or Automation prompt, then retry. Existing agents were not stopped; inspect cxl.") from exc
        if result.returncode:
            detail = ''.join(c if c.isprintable() else ' ' for c in result.stderr)[:400]
            raise StateError('iTerm2 automation failed. Check Automation permission and existing panes. ' + detail)
        return result.stdout.strip()

    def preflight(self, mode='window', anchor=''):
        self.call('preflight')
        # A bundle version is available even while first-launch UI is blocked.
        # Require actual session access before any new agent is launched.
        views = self.inventory()
        if mode == 'split' and not any(v['tty'] == anchor for v in views):
            raise StateError('--split must be invoked in a local iTerm2 pane. Use --window otherwise.')

    def inventory(self):
        return parse_views(self.call('inventory'))

    def focus(self, view):
        if self.call('focus', view['guid'], view['tty']) != 'focused':
            raise StateError('Focus was not confirmed; no replacement view created.')

    def present(self, view, display_name):
        display_name = name(display_name)
        if self.call('present', view['guid'], view['tty'], display_name) != 'presented':
            raise StateError('iTerm2 did not confirm the presentation update.')

    def inspect(self, view):
        parts = self.call('inspect', view['guid'], view['tty']).split('\t')
        if len(parts) != 4 or not parts[3].isdigit():
            raise StateError('Unrecognized iTerm2 presentation response.')
        return dict(terminal_title=parts[0], badge_name=parts[1],
                    session_name=parts[2], window_id=int(parts[3]))

    def open(self, commands, mode, anchor, per_tab=0, min_columns=70, min_rows=12, names=None):
        if not commands:
            return []
        names = list(names or commands)
        if len(names) != len(commands):
            raise StateError('Every iTerm2 command needs one presentation name.')
        pairs = [value for pair in zip(commands, names) for value in pair]
        result = parse_views(self.call('open', mode, anchor, min_columns, min_rows,
                                       per_tab, PROFILE_NAME, *pairs))
        if len(result) != len(commands):
            raise StateError('Partial GUI layout; agents retained. Inspect existing views before retrying.')
        return result


def client_map(b):
    rows = b.snapshot()['sessions']
    return {row['sid']: b.client_ttys(row) for row in rows}


def attachment(row, context):
    if context.get('backend') != 'zmx':
        raise StateError('Only verified zmx generations can be opened in active views.')
    generation = row.get('generation') or {}
    if any(key not in generation for key in ('runtime_dir', 'daemon_pid', 'created')) or not context.get('runtime_path'):
        raise StateError('Incomplete zmx generation; no view command created.')
    helper = str(Path(__file__).resolve().with_name('cx_zmx.py'))
    return shlex.join([sys.executable, helper, 'attach-verified', context['runtime_path'], generation['runtime_dir'],
                       row['session'], str(generation['daemon_pid']), str(generation['created'])])


def show(rows, b, store, gui=None, mode='window', anchor=None,
         per_tab=0, min_columns=70, min_rows=12):
    if not rows:
        return dict(opened=0, reused=0)
    if mode not in ('window', 'split', 'tab') or per_tab < 0 or min_columns < 20 or min_rows < 5:
        raise StateError('Invalid layout settings.')
    gui = gui or ITerm()
    anchor = caller_tty() if anchor is None else anchor
    configure = getattr(gui, 'configure', None)
    if configure:
        configure(store.preference('timestamps', True))
    gui.preflight(mode, anchor)
    with store.lock('views'):
        data = b.snapshot()
        context = data['context']
        current = {r['_key']: r for r in data['sessions']}
        selected = {r['_key']: r for r in rows}
        requested = list(selected)
        if any(k not in current for k in requested):
            raise StateError('A selected session changed/disappeared; nothing opened. Refresh the console.')
        views, clients = gui.inventory(), client_map(b)
        state = store.read()
        existing, missing = [], []
        for key in requested:
            row = current[key]
            display_name = (selected[key].get('display_name') or selected[key].get('task') or
                            row.get('display_name') or row.get('task') or row['session'])
            matches = [v for v in views if v['tty'] in clients.get(row['sid'], set())]
            if row.get('attached', 0) > 1 or len(matches) > 1:
                raise StateError('Multiple zmx clients already view this conversation. Close extras before cx opens or focuses a preferred view.')
            if matches:
                presenter = getattr(gui, 'present', None)
                if presenter:
                    presenter(matches[0], display_name)
                existing.append(matches[0])
                continue
            if row.get('attached', 0):
                raise StateError('A zmx client is attached but cannot be verified as an iTerm2 pane. No duplicate view opened.')
            cached = state['views'].get(key)
            if cached and any(v == cached for v in views):
                raise StateError('A cached iTerm2 pane has no verified zmx attachment. Inspect or close it; no duplicate opened.')
            missing.append((row, display_name))
        if missing:
            receipts = gui.open([attachment(r, context) for r, _ in missing], mode, anchor,
                                per_tab, min_columns, min_rows,
                                names=[display for _, display in missing])
            store.change(lambda s: s['views'].update({r['_key']: v for (r, _), v in zip(missing, receipts)}))
            deadline = time.monotonic() + 5
            while True:
                connected = client_map(b)
                if any(len(connected.get(r['sid'], set())) > 1 for r, _ in missing):
                    raise StateError('Multiple zmx clients attached while cx was opening a preferred view. Close extras; the agent process was retained.')
                if all(v['tty'] in connected.get(r['sid'], set()) for (r, _), v in zip(missing, receipts)):
                    break
                if time.monotonic() >= deadline:
                    raise StateError('Views created but attachment unconfirmed. Inspect them; agent processes were retained.')
                time.sleep(0.05)
        elif existing:
            gui.focus(existing[0])
        return dict(opened=len(missing), reused=len(existing))


def refresh(rows, b, store, gui=None):
    """Refresh presentation for verified existing views without opening a client."""
    gui = gui or ITerm()
    configure = getattr(gui, 'configure', None)
    if configure:
        configure(store.preference('timestamps', True))
    gui.preflight()
    data = b.snapshot()
    current = {r['_key']: r for r in data['sessions']}
    selected = {r['_key']: r for r in rows}
    requested = list(selected)
    if any(key not in current for key in requested):
        raise StateError('A selected session changed/disappeared; no presentation was changed.')
    views, clients = gui.inventory(), client_map(b)
    refreshed, missing = 0, 0
    for key in requested:
        row = current[key]
        matches = [v for v in views if v['tty'] in clients.get(row['sid'], set())]
        if row.get('attached', 0) > 1 or len(matches) > 1:
            raise StateError('Multiple views exist for one conversation; presentation refresh stopped.')
        if not matches:
            missing += 1
            continue
        desired = (selected[key].get('display_name') or selected[key].get('task') or
                   row.get('display_name') or row.get('task') or row['session'])
        gui.present(matches[0], desired)
        refreshed += 1
    return dict(refreshed=refreshed, missing=missing)
