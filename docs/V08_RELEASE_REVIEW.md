# v0.8 integrated release contract

CX Deck keeps four kinds of state separate: exact Codex conversation identity,
zmx runtime generation, native iTerm presentation, and private organization
metadata. The table records the maximum intended effect of each public command.
Read access includes bounded history, process, zmx, iTerm, or Store inspection.

| Command | Conversation / runtime | Presentation | Organization |
| --- | --- | --- | --- |
| `cx`, `cx new` | create one explicit runtime | attach/create requested view | record launch metadata |
| `cx resume` | reuse exact live generation or cold-resume selected exact UUID | reuse/create requested view | record launch/view metadata |
| `cx resume --list|--json` | read | read | read |
| `cx focus NAME` | read exact generation | focus verified view or create one missing view | view receipt may update after verification |
| `cx focus --next|--previous` | read | focus one existing verified view | read |
| `cx find`, `cx status`, dashboard refresh | read | read | read |
| `cx workspace save` | read | read | write membership/adaptive settings |
| `cx workspace capture` | read | read twice; never mutate | atomically write validated `exact_layout` |
| `cx workspace open` | reuse live; may exact-resume saved-only members after full preflight | exact MOVE_REUSE reconstruction | write verified view receipts |
| `cx workspace open --adaptive` | same exact resume rules | adaptive native view creation/reuse | write verified view receipts |
| `cx workspace open --no-iterm` | resolve required exact runtimes | no change | record successful cold launch metadata |
| `cx workspace open --list` | read | read | read |
| `cx views status` | read | read | read |
| `cx views refresh` | read; never relabel | refresh names/profile on verified existing views | repair independently verified receipts |
| `cx views rebuild` | read; never start or restart runtime | reuse verified views; create one only for `NO_VIEW` | write independently verified receipts |
| `cx rename`, pin/group commands | no change | rename may refresh a verified view | write requested metadata |
| `cx upgrade` | read compatibility; no restart/relabel | no change | no change |
| `cxkill` | destroy only the explicitly confirmed exact zmx generation | attached client ends with runtime | no identity rewrite |

`cx workspace open NAME` and `cx views rebuild --workspace NAME` are deliberately
different. Exact open reconstructs the saved window/tab/split tree. Scoped view
rebuild only ensures that selected live workspace members have safe native views;
it does not move an already verified view or claim topology restoration. Exact
restore never silently falls back to adaptive layout.

All presentation paths use nonbinding runtime snapshots. GUID/TTY receipts are
caches; fresh token-verified client and iTerm observations remain authoritative.
Presentation failure never kills, restarts, detaches, relabels, or replaces a
Codex process or zmx generation.

The v0.6-to-v0.7 state-directory rename runs only from the installer. Constructing
or reading the Store does not create a directory, alter permissions, or perform a
path upgrade; read-only commands therefore remain filesystem-neutral.
