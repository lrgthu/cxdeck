# CX Deck version upgrades

A session's `cx_version` label records the cx version that created that runtime
generation. It is immutable launch history. Installed control-plane code decides
compatibility and does not rewrite that label.

`cx upgrade status` compares:

- the installed cx version;
- the installed zmx version and minimum supported version;
- every managed session's launch-time cx version.

The centralized semantic-version function returns:

- `CURRENT`: session runtime semantics match the installed cx version.
- `UPGRADE_AVAILABLE`: the session was created by an older compatible version;
  normal operation remains allowed and no restart is required.
- `UPGRADE_REQUIRED`: a code-defined future boundary requires controlled
  regeneration. cx reports the affected sessions and does not restart them.
- `INCOMPATIBLE`: the label is malformed, predates the supported zmx runtime
  model, comes from a newer unsupported controller, or otherwise cannot be used
  safely.

The v0.7 compatibility table has no regeneration boundary. A live session with
`cx_version=0.6.0` is `UPGRADE_AVAILABLE`: it is fully usable and requires no
restart. A v0.7-created session is `CURRENT`. `cx upgrade` is declarative: it
reports current or compatible sessions, reports required future action, and
fails closed for incompatible sessions. It does not attach, detach, kill,
restart, rewrite a session label, or recreate a runtime generation.

The v0.7 local state-path upgrade moves only the durable workbench directory from
`~/.local/state/codex-tmux/workbench` to `~/.local/state/cxdeck/workbench` with an
atomic rename. It validates ownership and symlinks, is idempotent, and does not
inspect or change Codex or zmx. Any unrelated historical files at the old path
remain inert.
