# Releasing CX Deck

CX Deck uses ordinary public Git history. The public repository is the canonical
development history; never import Git objects from historical private
repositories into this graph.

## Version policy

- Patch releases contain bug and compatibility fixes.
- Minor releases add backward-compatible features.
- Major releases may change conversation identity, runtime compatibility, or
  the public CLI contract.

The version lives in `cx_version.py`. A release updates that value and
`CHANGELOG.md` in the same pull request. A controller version change does not by
itself require running zmx generations to restart; runtime requirements remain
an explicit compatibility decision in `cx_upgrade.py`.

## Release checks

Run from a clean release branch:

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

Run the six disposable real-zmx tests and `tools/zmx_behavior_probe.py`. When a
change affects presentation, also run the applicable disposable real-iTerm
capture, MOVE_REUSE, restore, and navigation acceptance tools. These tests must
use temporary homes, private zmx runtime directories, dummy processes, and
disposable iTerm views. Never use active user sessions as fixtures.

Review the privacy audit, the complete pull-request diff, and the actual regular
CI job conclusions. Merge through a pull request. Then create and push an
annotated version tag and publish a GitHub Release from that exact tag. Never
move, replace, or delete an existing published version tag.

Exact workspace tests require the explicitly installed `iterm2==2.23` package.
The package remains optional for ordinary CX Deck use.
