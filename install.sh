#!/bin/zsh
emulate -L zsh
set -e
if ! command -v python3 >/dev/null 2>&1; then
  print -u2 -- 'Python 3.9+ is required. On macOS: brew install python'
  exit 1
fi
command python3 "${0:A:h}/install.py"
