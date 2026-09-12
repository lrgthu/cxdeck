# Contributing

For a focused change, fork the repository or create a branch, add relevant
tests, and open a pull request. Discuss large behavioral changes in an issue
first. Keep pull requests small enough to review as one safety contract.

Preserve these architecture boundaries:

```text
Codex UUID = durable conversation identity
zmx        = persistent PTY/runtime only
iTerm2     = native presentation
CX Deck    = control plane
```

Do not substitute titles, paths, PIDs, zmx names, or iTerm identifiers for exact
conversation identity. Do not add prompt injection, cross-pane input, terminal
content scraping, hidden package installation, or automatic session destruction.

Run before opening a pull request:

```zsh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
zsh -n cxdeck.zsh
zsh -n install.sh
zsh -n uninstall.sh
zsh -n tests/prepare_ci_iterm.zsh
python3 tools/public_release_audit.py
git diff --check
```

Real zmx and iTerm checks must use disposable runtime directories, fake Codex
processes, temporary homes, and disposable iTerm views. Never use a person's
active sessions as fixtures. Describe which disposable integration checks you
ran in the pull request.

See [the release procedure](docs/RELEASING.md) for maintainer-only release gates.
The public repository is the canonical development history. Never import Git
objects from historical private repositories into this graph.
