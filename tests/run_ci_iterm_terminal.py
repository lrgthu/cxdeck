#!/usr/bin/env python3
"""CI-only: run the unchanged GUI test FROM an actual iTerm2 terminal.

This matches cx's normal caller (iTerm2), rather than the hosted CI agent
attempting to control another application. No TCC/Automation grants are changed.
Only the disposable runner's iTerm profile preferences are configured.
"""
from pathlib import Path
import os
import plistlib
import shlex
import subprocess
import sys
import time
import uuid


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform != 'darwin':
        raise RuntimeError('Restricted to disposable macOS GitHub runners. Run iterm_e2e.py locally.')
    if subprocess.run(['pgrep', '-x', 'iTerm2'], capture_output=True).returncode == 0:
        raise RuntimeError('iTerm2 already runs; refusing to replace an existing test environment.')
    root = Path(__file__).resolve().parents[1]
    folder = Path(os.environ['RUNNER_TEMP']) / ('cx-terminal-' + uuid.uuid4().hex[:12])
    folder.mkdir(mode=0o700)
    status = folder / 'status.txt'
    log = folder / 'test.log'
    wrapper = folder / 'run-test.zsh'
    env = ['env', 'GITHUB_ACTIONS=true', 'RUNNER_TEMP=' + os.environ['RUNNER_TEMP'],
           'PATH=' + os.environ['PATH'],
           'CX_E2E_VERIFY_PYTHON_API=' + os.environ.get('CX_E2E_VERIFY_PYTHON_API', '0'),
           sys.executable, '-u', str(root / 'tests/iterm_ci_diagnostics.py')]
    wrapper.write_text('#!/bin/zsh\ncd ' + shlex.quote(str(root)) + ' || exit\n' +
        shlex.join(env) + ' >' + shlex.quote(str(log)) + ' 2>&1\n' +
        'rc=$?\nprintf "%s\\n" "$rc" >' + shlex.quote(str(status)) + '\nexit "$rc"\n')
    wrapper.chmod(0o700)
    guid = str(uuid.uuid4())
    prefs = folder / 'iterm.plist'
    profile = {'Name': 'CX isolated GUI acceptance', 'Guid': guid,
        'Custom Command': 'Yes', 'Command': shlex.join(['/bin/zsh', '-f', str(wrapper)]),
        'Custom Directory': 'Yes', 'Working Directory': str(root), 'Rows': 56, 'Columns': 180}
    with prefs.open('wb') as stream:
        plistlib.dump({'New Bookmarks': [profile], 'Default Bookmark Guid': guid}, stream)
    subprocess.run(['defaults', 'import', 'com.googlecode.iterm2', str(prefs)], check=True)
    subprocess.run(['open', '-n', '/Applications/iTerm.app'], check=True)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and not status.exists():
        time.sleep(0.2)
    text = log.read_text(errors='replace') if log.exists() else 'No terminal test log was created.'
    print(text, flush=True)
    if not status.exists():
        raise RuntimeError('iTerm2 terminal acceptance did not finish; inspect the CI screenshot. No permission modified.')
    code = status.read_text().strip()
    if code != '0':
        raise RuntimeError('Actual terminal-launched GUI acceptance failed with exit ' + code)
    if '"result": "PASS"' not in text:
        raise RuntimeError('Expected explicit GUI acceptance result missing.')
    print('CI graphical acceptance completed from an actual iTerm2 terminal.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('GUI terminal harness FAILED/BLOCKED: ' + str(exc), file=sys.stderr)
        folder = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / 'cx-gui-diagnostics'
        folder.mkdir(exist_ok=True)
        try:
            subprocess.run(['screencapture', '-x', str(folder / 'terminal-screen.png')], capture_output=True, timeout=10)
        except Exception:
            pass
        sys.exit(1)
