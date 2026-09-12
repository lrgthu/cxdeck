# Contributing

Open an issue before a large behavioral change. Keep zmx limited to persistent
PTY lifecycle, keep iTerm2 responsible for presentation, and preserve exact
Codex UUID identity. Do not add prompt injection, cross-pane input, terminal
content scraping, or hidden package installation.

Run before opening a pull request:

```zsh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
zsh -n cxdeck.zsh
zsh -n install.sh
zsh -n uninstall.sh
zsh -n tests/prepare_ci_iterm.zsh
git diff --check
```

Real zmx and iTerm checks must use disposable runtime directories, fake Codex
processes, and temporary homes. Never use a person's active sessions as fixtures.
