#!/bin/zsh
emulate -L zsh
set -e
if ! command -v python3 >/dev/null 2>&1; then
  print -u2 -- 'Python 3.9+ is required.'
  exit 1
fi
command python3 - "${0:A:h}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from install import uninstall

uninstall(Path.home())
print('CX Deck was removed. Codex history, CX Deck state, and running zmx sessions were left untouched.')
PY
