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
- Persistent pane badges keep long conversation names visible in narrow layouts.
- Native iTerm2 timestamps show when scrollback lines were last modified.
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

The fully tested v0.7 environment is macOS with iTerm2, Python 3.9 or newer,
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
cx upgrade status
```

The installer checks prerequisites and installs CX Deck under
`~/.local/share/cxdeck`. It does not install zmx, restart Codex, or modify a
running zmx session. It creates only the `CX Deck` iTerm dynamic profile, which
inherits the user's default profile and supplies CX Deck-scoped badges and
timestamps.

To update an existing checkout:

```zsh
git pull --ff-only
./install.sh
```

## Quick start

```zsh
cx                         # start a persistent Codex session here
cx "Model Evaluation"     # start or reuse an exact display name
cx new --split             # new native iTerm2 split
cx resume                  # select from saved and live conversations
cx focus "Model Evaluation"
cx status
```

Closing the pane detaches the view. The Codex process remains in its zmx PTY.
`cx focus`, `cx resume`, or `cx views rebuild` restores a verified native view.

## Core commands

```text
cx new [--split|--tab|--window] [--count N]
cx resume [--safe|--yolo] [--select EXACT_UUID ...] [--no-iterm]
cx focus EXACT_NAME
cx rename EXACT_NAME DISPLAY_NAME
cx pin|unpin EXACT_NAME
cx group ...
cx workspace ...
cx views rebuild|refresh
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

## Native iTerm experience

CX Deck-created views use a native iTerm2 session badge backed by the
`user.cxdeck_name` session variable, with native session-name metadata as a
supplement. `cx rename` updates verified current views without reconnecting
Codex. At very narrow widths
iTerm2 may visually truncate presentation, while the untruncated CX Deck name
remains in the native session variable and session metadata.

Native iTerm2 scrollback timestamps are enabled by default only in the CX Deck
profile. Toggle CX Deck views with:

```zsh
cx config timestamps off
cx config timestamps on
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

v0.6 zmx sessions are fully usable under v0.7 and report
`UPGRADE_AVAILABLE`. Their launch-time labels remain truthful and unchanged.

## Architecture

See [Architecture](docs/ARCHITECTURE.md), [zmx runtime](docs/ZMX_RUNTIME.md),
[iTerm presentation](docs/ITERM_PRESENTATION.md), and
[version upgrades](docs/VERSION_UPGRADES.md).

## Troubleshooting

Run `cx doctor` first. It reports Codex and zmx paths and versions, managed
session counts, launch policies, unidentified external processes, iTerm2
automation, and upgrade compatibility. GUI errors leave Codex and zmx running;
use `cx status` to inspect runtime health and `cx views rebuild` to reconstruct
missing views.

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
