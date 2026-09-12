# CX Deck architecture

CX Deck has one persistence provider: zmx. The boundaries are deliberately narrow.

```text
Codex UUID  owns durable conversation identity
zmx         owns persistent PTY lifetime and client attachment
iTerm2      owns native terminal presentation
CX Deck     owns lifecycle, history, organization, views and upgrades
```

Conversation identity is the tuple `(host, realpath(CODEX_HOME), exact Codex
UUID)`. A title, repository, cwd, workspace, iTerm identifier, PID, or zmx
session name cannot substitute for it. Human-readable names are presentation
metadata and never participate in matching, duplicate detection, or resume.

A live zmx generation is the tuple `(host, realpath(socket_dir), exact session
name, daemon PID, creation timestamp)`. All fields must match before cx attaches
to a known generation. PID and name alone are insufficient.

Saved conversation metadata comes only from Codex app-server `thread/list`.
Live inventory comes from cx-managed zmx labels plus exact process ancestry.
External Codex processes remain outside cx ownership. Known same-UUID copies
block cold resume; unidentified copies trigger the explicit uncertainty policy.

iTerm GUIDs and TTYs verify a current view. They remain presentation metadata.
A presentation failure cannot stop or replace a zmx generation. cx maintains one
preferred verified view per managed conversation and reconstructs missing views
with native windows, tabs, and splits.

Display names, pins, groups, workspaces, layouts, preferences, and view receipts
live in the private Store. Runtime generation keys and durable conversation keys remain
separate so presentation and organization changes cannot alter identity.
