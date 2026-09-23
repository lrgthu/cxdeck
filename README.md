# CX Deck

**Persistent Codex sessions with a native terminal experience.**

CX Deck keeps interactive Codex sessions alive when an iTerm2 pane closes. Open
or reconstruct a view later and continue the exact conversation. Scrolling,
selection, copy and paste, mouse input, resizing, colors, links, and full-screen
redraw remain native iTerm2 behavior.

CX Deck is a control plane around the Codex CLI. It is not a terminal
multiplexer, a replacement for Codex, or an agent messaging system.

## Why CX Deck

- Closing a terminal pane does not kill Codex.
- Exact Codex UUIDs prevent fuzzy or accidental resume.
- Native iTerm2 windows, tabs, and splits stay the presentation layer.
- Native iTerm2 session titles provide compact conversation labels.
- Optional native iTerm2 timestamps show when scrollback lines were last modified.
- Groups, pins, display names, workspaces, and layouts stay local and private.

## How it works

```text
Codex UUID  -> durable conversation identity
zmx         -> persistent PTY lifetime and attachment
iTerm2      -> windows, tabs, splits, scrollback, input, and rendering
CX Deck     -> lifecycle, history, organization, views, diagnostics, upgrades
```

CX Deck uses zmx as an implementation dependency. Users normally interact only
with `cx`.

## Install

The fully tested v0.8 environment is macOS with iTerm2, Python 3.9 or newer,
zsh, Git, the Codex CLI, and zmx 0.8.1 or newer. Headless zmx sessions work
without iTerm2, but native layouts, names, and timestamps require iTerm2.

Install third-party dependencies explicitly. For zmx:

```zsh
brew install neurosnap/tap/zmx
```

Clone this repository, then run:

```zsh
git clone https://github.com/lrgthu/cxdeck.git
cd cxdeck
./install.sh
source "$HOME/.cxdeck.zsh"
cx doctor
cx
```

The installer checks prerequisites and installs CX Deck under
`~/.local/share/cxdeck`. It does not install zmx, restart Codex, or modify a
running zmx session. It creates only the `CX Deck` iTerm dynamic profile, which
inherits the user's default profile and supplies the CX Deck-scoped timestamp
preference.

To update an existing checkout:

```zsh
git pull --ff-only
./install.sh
```

## Quick start

```zsh
cx                         # start a persistent Codex session here
cx --yolo                  # start a persistent Codex session in YOLO mode
cx "Model Evaluation"     # start or reuse an exact display name
cx new --split             # new native iTerm2 split
cx new --split -- --yolo   # explicit split with Codex launch flags
cx resume                  # select from saved and live conversations
cx status
cx dashboard

cx workspace save daily
cx workspace capture daily # optional exact topology support
cx workspace open daily

cx views status
cx find evaluation
cx focus --next
```

Closing the pane detaches the view. The Codex process remains in its zmx PTY.
`cx focus`, `cx resume`, or `cx views rebuild` restores a verified native view.

## Core commands

```text
cx [CODEX_FLAGS...]
cx new [--split|--tab|--window] [--count N]
cx resume [--safe|--yolo] [--select EXACT_UUID ...] [--no-iterm]
cx focus EXACT_NAME
cx rename EXACT_NAME DISPLAY_NAME
cx pin|unpin EXACT_NAME
cx group ...
cx workspace save|capture|open|list ...
cx views status [--json] [--workspace NAME]
cx views rebuild|refresh [--workspace NAME]
cx find QUERY
cx focus --next|--previous
cx config timestamps on|off
cx dashboard
cx status [--json]
cx doctor
cx upgrade [status [--json]]
cxkill EXACT_NAME
```

Cold resume defaults to the established YOLO policy. Use `cx resume --safe` or
`--no-yolo` for the configured Codex safety policy. Reusing a live session never
restarts it because a different policy was requested.

## Session identity and safety

Conversation identity is exactly:

```text
(host, realpath(CODEX_HOME), exact Codex UUID)
```

Titles, working directories, repositories, PIDs, iTerm GUIDs, zmx names, groups,
and workspace membership are not conversation identity. A known external
same-UUID Codex process blocks cold resume. Unidentified live Codex processes
fail closed unless the user explicitly accepts that uncertainty. Two managed
live generations claiming one UUID are a hard error.

CX Deck does not parse terminal contents to identify conversations and does not
send prompts or cross-pane input. Termination requires the explicit `cxkill`
flow and exact generation verification.

## Workspaces and groups

Display names, pins, groups, workspace membership, and layout metadata live in
`~/.local/state/cxdeck`. They organize exact conversations without changing
their UUID or runtime generation. A workspace records a provider-neutral native
layout plan and reconstructs verified views without nesting another terminal UI.
`cx workspace capture NAME [--replace]` can add an optional exact native split
tree after double-read verification. This feature lazily requires the explicitly
installed `iterm2==2.23` Python package; ordinary CX Deck commands and
membership-only workspaces do not. Install that optional support into the same
Python used by `cx` with `python3 -m pip install --user 'iterm2==2.23'`.
Opening a captured workspace resolves every exact UUID and checks duplicates
before moving verified iTerm Sessions into the saved tree. Frames and ratios are
best-effort hints. Use `cx workspace open NAME --adaptive` to deliberately use
the older adaptive layout; exact restore never falls back to it silently.

`cx status`, `cx resume --json`, and `cx views status --json` use the normalized
`cxdeck.inventory/v1` model. Conversation, runtime, external-process, and view
health are reported separately. `cx find QUERY` searches local display name,
group, cwd, and UUID metadata only; it never reads transcripts or terminal
contents. `cx focus --next|--previous` navigates existing verified views without
opening a client. Workspace-scoped `views refresh` and `views rebuild` repair
presentation only and never resume a saved conversation.

## Native iTerm experience

CX Deck supplies each view's native iTerm2 session title. `cx rename` updates
verified current views without reconnecting Codex. To show compact labels for
split panes, enable this user-owned iTerm2 preference:

```text
iTerm2 Settings
→ Appearance
→ Panes
→ Show per-pane title bar with split panes
```

CX Deck does not change that global preference and does not draw labels inside
terminal output.

Native iTerm2 scrollback timestamps are off by default. Enable them for CX Deck
views with:

```zsh
cx config timestamps on
cx config timestamps off
```

No timestamp text is inserted into Codex output. CX Deck does not configure
terminal mouse modes, copy mode, `TERM`, clipboard behavior, selection, or keys.
Every managed client sets `ZMX_NO_DETACH_KEY=1`, so zmx reserves no detach key.

## Version upgrades

`cx upgrade status` compares the installed controller, zmx version, and each
session's launch-time `cx_version` label. A version mismatch alone never implies
a restart:

- `CURRENT`: current runtime semantics.
- `UPGRADE_AVAILABLE`: older but fully compatible; no restart required.
- `UPGRADE_REQUIRED`: a future explicit compatibility boundary needs a defined
  action; CX Deck reports it and does not restart automatically.
- `INCOMPATIBLE`: CX Deck cannot safely assume the runtime contract and fails
  closed for affected operations.

v0.6 and v0.7 zmx sessions are fully usable under v0.8 and report
`UPGRADE_AVAILABLE`. Their launch-time labels remain truthful and unchanged;
the v0.8 workspace and navigation features require no runtime regeneration.

## Architecture

See [Architecture](docs/ARCHITECTURE.md), [zmx runtime](docs/ZMX_RUNTIME.md),
[iTerm presentation](docs/ITERM_PRESENTATION.md), and
[version upgrades](docs/VERSION_UPGRADES.md).

## Troubleshooting

Run `cx doctor` first. It reports Codex and zmx paths and versions, managed
session counts, launch policies, unidentified external processes, iTerm2
automation, and upgrade compatibility. GUI errors leave Codex and zmx running;
use `cx status` to inspect runtime health and `cx views rebuild` to reconstruct
missing views. Before posting diagnostics publicly, remove sensitive hostnames,
paths, UUIDs, PIDs, and project names.

## Uninstall

```zsh
./uninstall.sh
```

Uninstall removes CX Deck code, its shell hook, and its owned iTerm profile. It
does not delete Codex history, `~/.local/state/cxdeck`, or running zmx sessions.

## Security and privacy

CX Deck stores compact local metadata, never transcript bodies. Paths in zmx
labels are encoded for syntax safety, not encrypted. See [SECURITY.md](SECURITY.md)
for reporting guidance and supported-version policy.

## Independence

CX Deck is an independent, unofficial project. It is not affiliated with or
endorsed by OpenAI, iTerm2, or the zmx project. Codex, iTerm2, and zmx are the
products of their respective owners.

## License

[MIT](LICENSE)
