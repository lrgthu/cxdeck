# Changelog

## 0.8.1

- Added exact native iTerm workspace capture and ordered split-tree restoration;
  split ratios and window frames remain best-effort hints.
- Reuse and move verified iTerm Sessions without restarting Codex or replacing
  zmx generations.
- Added unified conversation/runtime/view health, metadata-only search,
  next/previous focus, and workspace-scoped presentation repair.
- Materialize iTerm session GUID and TTY values before serializing live view
  inventory, avoiding an AppleScript coercion race immediately after supported
  session moves.
- Allow disposable exact-restore acceptance enough time for iTerm to finish an
  asynchronous presentation-client close.

## 0.7.0

- Renamed the product to CX Deck and prepared the repository for public review.
- Added CX Deck-scoped native iTerm2 names and scrollback timestamps.
- Added an atomic, idempotent v0.6 state-directory upgrade with no runtime restart.
- Kept v0.6 zmx runtime generations fully compatible.

## 0.6.0

- Established zmx as the sole persistent PTY provider.
- Kept iTerm2 as the native presentation layer and Codex UUID as conversation identity.
- Added organization, view reconstruction, diagnostics, and version compatibility.
