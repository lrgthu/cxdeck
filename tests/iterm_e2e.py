#!/usr/bin/env python3
"""Opt-in real iTerm2 acceptance using only private zmx and fake Codex state."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_console
import console_entry
import cx_iterm
from cx_store import Store
import workbench

CLOSE = '''on run argv
  tell application id "com.googlecode.iterm2"
    set targetSession to missing value
    set ws to get windows
    repeat with wi from 1 to count of ws
      set w to item wi of ws
      set ts to get tabs of w
      repeat with ti from 1 to count of ts
        set t to item ti of ts
        set ss to get sessions of t
        repeat with si from 1 to count of ss
          set s to item si of ss
          if (unique id of s) is (item 1 of argv) and (tty of s) is (item 2 of argv) then
            set targetSession to s
            exit repeat
          end if
        end repeat
        if targetSession is not missing value then exit repeat
      end repeat
      if targetSession is not missing value then exit repeat
    end repeat
    if targetSession is missing value then return "absent"
    tell targetSession to close
    return "closed"
  end tell
end run
'''

STUB = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    const char *file = getenv("FAKE_CODEX_ENV_LOG");
    const char *name = getenv("CX_AGENT_NAME");
    const char *no_detach = getenv("ZMX_NO_DETACH_KEY");
    if (!name) name = "";
    if (!no_detach) no_detach = "";
    if (file) {
        FILE *f = fopen(file, "a");
        if (!f) return 8;
        fprintf(f, "%s\t%s\n", name, no_detach);
        fclose(f);
    }
    printf("\033[?1049h\033[32mCX disposable zmx/iTerm acceptance\033[0m\r\n");
    for (int i = 0; i < 24; ++i) {
        printf("timestamp-line-%02d\r\n", i);
        fflush(stdout);
        usleep(20000);
    }
    fflush(stdout);
    for (;;) sleep(1);
}
'''


def close(view):
    result = subprocess.run(["osascript", "-", view["guid"], view["tty"]], input=CLOSE,
                            text=True, capture_output=True, timeout=15)
    if result.returncode or result.stdout.strip() not in ("closed", "absent"):
        raise RuntimeError("Could not close a recorded test view: " + result.stderr)


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("Timed out waiting for disposable iTerm/zmx state")


def view_identities(rows):
    return {(row["guid"], row["tty"]) for row in rows}


def resize(view, columns):
    script = '''on run argv
      tell application id "com.googlecode.iterm2"
        set ws to get windows
        repeat with wi from 1 to count of ws
          set w to item wi of ws
          set ts to get tabs of w
          repeat with ti from 1 to count of ts
            set t to item ti of ts
            set ss to get sessions of t
            repeat with si from 1 to count of ss
              set s to item si of ss
              if (unique id of s) is (item 1 of argv) and (tty of s) is (item 2 of argv) then
                set columns of s to ((item 3 of argv) as integer)
                return "resized"
              end if
            end repeat
          end repeat
        end repeat
      end tell
      error "Disposable pane disappeared"
    end run'''
    result = subprocess.run(['osascript', '-', view['guid'], view['tty'], str(columns)], input=script,
                            text=True, capture_output=True, timeout=15)
    if result.returncode or result.stdout.strip() != 'resized':
        raise RuntimeError('Could not resize disposable pane: ' + result.stderr)


def verify_profile_api(views, timestamps, api_environment):
    """Optionally verify live session profile properties with iTerm's Python API."""
    if os.environ.get('CX_E2E_VERIFY_PYTHON_API') != '1':
        return False
    import iterm2

    async def inspect(connection):
        app = await iterm2.async_get_app(connection)
        for view in views:
            session = app.get_session_by_id(view['guid'])
            if session is None:
                raise RuntimeError('Disposable iTerm session disappeared before profile verification')
            properties = (await session.async_get_profile()).all_properties
            if properties.get('Badge Text') != r'\(user.cxdeck_name)':
                raise RuntimeError('Live iTerm session did not retain the CX Deck badge profile property')
            if ('Timestamps Visible' not in properties or
                    bool(properties['Timestamps Visible']) != timestamps):
                observed = {key: value for key, value in properties.items()
                            if 'Timestamp' in key or key == 'Guid'}
                raise RuntimeError('Live iTerm session timestamp property differs from the CX Deck preference: '
                                   + repr(observed))

    with patch.dict(os.environ, api_environment, clear=False):
        iterm2.run_until_complete(inspect)
    return True


def main():
    zmx_source = os.environ.get("ZMX_TEST_BINARY") or shutil.which("zmx")
    if sys.platform != "darwin" or not zmx_source or not all(
            shutil.which(tool) for tool in ("cc", "osascript")):
        raise RuntimeError("This opt-in test requires macOS, iTerm2, zmx 0.8.1+ and a C compiler.")
    gui = cx_iterm.ITerm()
    api_environment = {'HOME': str(Path.home()), **{
        key: value for key, value in os.environ.items() if key.startswith('ITERM')}}
    profile_path = cx_iterm._profile_path()
    timestamps = True
    if profile_path.exists():
        profile = json.loads(profile_path.read_text())['Profiles'][0]
        if profile.get('Guid') != cx_iterm.PROFILE_GUID:
            raise RuntimeError('Existing CX Deck profile is not owned by this installation')
        timestamps = bool(profile.get('Timestamps Visible', True))
    gui.configure(timestamps)
    gui.preflight()
    runtime = "/tmp/cxgi-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="cx-gui-e2e-", dir="/tmp") as temporary:
        home = Path(temporary).resolve()
        binary_dir = home / "bin"
        binary_dir.mkdir()
        shutil.copy2(zmx_source, binary_dir / "zmx")
        (home / "stub.c").write_text(STUB)
        subprocess.run(["cc", str(home / "stub.c"), "-o", str(binary_dir / "codex")],
                       check=True, capture_output=True)
        env_log = home / "client-env.log"
        env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / "codex-home"),
                   ZMX_DIR=runtime, ZMX_DIR_MODE="0700", ZMX_LOG_MODE="0600",
                   FAKE_CODEX_ENV_LOG=str(env_log),
                   PATH=str(binary_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
        for key in ("ZMX_SESSION", "ZMX_SESSION_PREFIX", "CX_MANAGED", "CX_AGENT_NAME"):
            env.pop(key, None)
        store = Store(home / ".local/state/cxdeck/workbench")
        agent_console.VERSION = console_entry.VERSION
        backend = console_entry.ResumeBackend(agent_console, store)
        owned, cleanup_errors = {}, []
        with patch.dict(os.environ, env, clear=True), \
             patch.object(agent_console.os, 'getcwd', return_value=str(home)):
            try:
                labels = ['Visual Encoding Study', 'Model Evaluation', 'Family Archive Demo',
                          'Research Project', 'Posterior Analysis']
                for label in labels:
                    agent_console.start_agent([], label, detached=True)
                rows = backend.snapshot()["sessions"]
                rows.sort(key=lambda item: labels.index(item['display_name']))
                if len(rows) != 5 or not all(row["state"] == "ALIVE" for row in rows):
                    raise RuntimeError("Five fake Codex processes were not verified alive")
                original = {row["session"]: (row["generation"], row["codex_pids"])
                            for row in rows}
                workbench.focus_rows(rows[:3], backend, mode='window')
                first_views = store.read()['views']
                anchor = first_views[rows[0]['_key']]['tty']
                workbench.focus_rows([rows[3]], backend, mode='tab', anchor=anchor)
                workbench.focus_rows([rows[4]], backend, mode='window')
                owned.update(store.read()["views"])
                before = view_identities(gui.inventory())
                agent_console._attach_or_focus(rows[0], rows[0]['display_name'], backend.core)
                if view_identities(gui.inventory()) != before:
                    raise RuntimeError('Direct attach command duplicated an already verified preferred view')
                if workbench.focus_rows(rows, backend) != {"opened": 0, "reused": 5}:
                    raise RuntimeError("Verified iTerm views were not reused")
                if view_identities(gui.inventory()) != before:
                    raise RuntimeError("Repeated focus duplicated or moved an iTerm view")

                workbench.annotate(rows[0]["session"], backend, title="Visual Computation Convergence")
                expected = ['Visual Computation Convergence', *labels[1:]]
                refreshed = backend.snapshot()['sessions']
                refreshed.sort(key=lambda item: expected.index(item['display_name']))
                for item in refreshed:
                    view = store.read()['views'][item['_key']]
                    presentation = gui.inspect(view)
                    if any((presentation['badge_name'] != item['display_name'],
                            presentation['session_name'] != item['display_name'])):
                        raise RuntimeError('Incorrect iTerm title/badge/profile assignment: ' + repr(presentation))
                profile_api_verified = verify_profile_api(
                    list(store.read()['views'].values()), timestamps, api_environment)
                long_view = store.read()['views'][refreshed[0]['_key']]
                for columns in (160, 100, 80, 60):
                    resize(long_view, columns)
                    presentation = gui.inspect(long_view)
                    if presentation['badge_name'] != 'Visual Computation Convergence':
                        raise RuntimeError(f'Full presentation name lost at {columns} columns')
                gui.focus(long_view)
                screenshot = os.environ.get('CX_E2E_SCREENSHOT')
                if screenshot:
                    captured = subprocess.run(
                        ['screencapture', '-x', '-l' + str(presentation['window_id']), screenshot],
                        capture_output=True, timeout=15)
                    if captured.returncode:
                        print('VISUAL CAPTURE SKIPPED: macOS Screen Recording permission is unavailable.')
                hold = float(os.environ.get('CX_E2E_VISUAL_HOLD', '0'))
                if hold > 0:
                    print(f'VISUAL FIXTURE READY: holding disposable panes for {hold:g} seconds.', flush=True)
                    time.sleep(min(hold, 120))
                workbench.workspace_save("test-group", backend)
                workbench.workspace_command(["open", "test-group", "--all", "--no-dashboard"], backend)
                if view_identities(gui.inventory()) != before:
                    raise RuntimeError("Workspace reopen duplicated an iTerm view")

                closed = store.read()["views"][refreshed[0]["_key"]]
                closed_session = refreshed[0]["session"]
                close(closed)
                wait_for(lambda: closed not in gui.inventory())
                wait_for(lambda: next(row for row in backend.snapshot()["sessions"]
                                      if row["session"] == closed_session)["attached"] == 0)
                live = {row["session"]: (row["generation"], row["codex_pids"])
                        for row in backend.snapshot()["sessions"]}
                if any(live[name] != identity for name, identity in original.items()):
                    raise RuntimeError("Closing an iTerm pane changed a zmx generation or Codex PID")
                if workbench.focus_rows([refreshed[0]], backend) != {"opened": 1, "reused": 0}:
                    raise RuntimeError("A missing preferred view was not rebuilt")
                owned.update(store.read()["views"])
                rebuilt = store.read()['views'][refreshed[0]['_key']]
                rebuilt_presentation = gui.inspect(rebuilt)
                if rebuilt_presentation['badge_name'] != 'Visual Computation Convergence':
                    raise RuntimeError('Rebuilt view lost the CX Deck presentation name')
                final = backend.snapshot()["sessions"]
                if len(final) != 5:
                    raise RuntimeError("Native split/tab creation did not produce five sessions")
                if any(next(row for row in final if row["session"] == name)["codex_pids"] != identity[1]
                       for name, identity in original.items()):
                    raise RuntimeError("Presentation changes restarted an existing Codex process")
                lines = env_log.read_text().splitlines()
                if len(lines) != 5 or any(not line.endswith("\t1") for line in lines):
                    raise RuntimeError("A managed zmx client omitted ZMX_NO_DETACH_KEY=1")
                print(json.dumps({"result": "PASS", "agents": 5, "zmx_version": "0.8.1",
                    "timestamps_enabled": timestamps,
                    "profile_api_verified": profile_api_verified,
                    "real_gui": True, "checks": ["native windows/splits/tabs", "five unique badges/titles",
                    "160/100/80/60-column name metadata", "native timestamp profile", "verified view reuse",
                    "one preferred view", "direct attach focuses preferred view", "rename and workspace reopen", "close pane keeps PID",
                    "recreate missing view", "ZMX_NO_DETACH_KEY=1"]}))
            finally:
                owned.update(store.read().get("views", {}))
                for view in {item["guid"]: item for item in owned.values()}.values():
                    try:
                        close(view)
                    except (RuntimeError, subprocess.TimeoutExpired) as exc:
                        cleanup_errors.append(str(exc))
                try:
                    result = subprocess.run([str(binary_dir / "zmx"), "list", "--short"],
                                            env=env, text=True, capture_output=True, timeout=5)
                    names = [line.strip().removeprefix("→ ") for line in result.stdout.splitlines()
                             if line.strip()]
                    if names:
                        subprocess.run([str(binary_dir / "zmx"), "kill", *names, "--force"],
                                       env=env, capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError) as exc:
                    cleanup_errors.append(str(exc))
                shutil.rmtree(runtime, ignore_errors=True)
                if cleanup_errors:
                    raise RuntimeError("Test-only cleanup unconfirmed: " + "; ".join(cleanup_errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("GUI E2E FAILED/BLOCKED: " + str(exc), file=sys.stderr)
        sys.exit(1)
