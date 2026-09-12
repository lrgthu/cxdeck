"""CI-only tracing of the real GUI test; failures remain failures."""
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from unittest.mock import patch

import iterm_e2e
import cx_iterm

original = cx_iterm.ITerm.call


def traced(self, *args):
    operation = str(args[0]) if args else 'empty'
    started = time.monotonic()
    print('[GUI] start ' + operation, flush=True)
    try:
        result = original(self, *args)
        print(f'[GUI] done {operation} in {time.monotonic()-started:.2f}s', flush=True)
        return result
    except Exception:
        print(f'[GUI] failed {operation} in {time.monotonic()-started:.2f}s', flush=True)
        raise


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('CI diagnostics are for the isolated GitHub runner; use iterm_e2e.py locally.')
    try:
        with patch.object(cx_iterm.ITerm, 'call', traced):
            iterm_e2e.main()
    except Exception:
        traceback.print_exc()
        folder = Path(os.environ['RUNNER_TEMP']) / 'cx-gui-diagnostics'
        folder.mkdir(exist_ok=True)
        try:
            p = subprocess.run(['screencapture', '-x', str(folder / 'screen.png')], capture_output=True, text=True, timeout=10)
            (folder / 'capture-status.txt').write_text(f'exit={p.returncode}\n{p.stderr}')
        except Exception as exc:
            (folder / 'capture-status.txt').write_text(str(exc))
        sys.exit(1)
