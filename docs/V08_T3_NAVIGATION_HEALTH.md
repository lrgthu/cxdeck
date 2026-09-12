# v0.8 T3: unified navigation and view health

T3 composes one read-only snapshot of saved Codex metadata, managed zmx
generations, external Codex processes, iTerm sessions, and private CX Deck
organization metadata. Commands use this common model instead of deriving
their own meanings for “live” or “visible.” No transcript or terminal content
is read.

## Inventory contract

The JSON schema identifier is `cxdeck.inventory/v1`. Each durable conversation
record has three independent state dimensions:

| Dimension | Meaning |
| --- | --- |
| `conversation_state` | Whether exact UUID metadata is saved, managed-live, external-live, both saved and live, or an unbound live-only runtime |
| `runtime_state` | Observed process fact for `runtime_source`: `ALIVE`, `STOPPED`, `NO_CODEX`, `UNKNOWN`, or `ABSENT` |
| `view_state` | Health of the native presentation binding, independent of runtime health |

Each record contains the exact `(host, realpath(CODEX_HOME), UUID)` identity
when known; display name, pin, group, and workspace names; saved-history
metadata; normalized zmx generation and process facts; known external PIDs;
and current view facts. A managed generation with no verified UUID is
`LIVE_ONLY_UNBOUND` and has no fabricated durable identity. Unidentified Codex
PIDs remain at inventory scope because CX Deck cannot truthfully attach them to
a conversation.

`runtime_source` is `managed`, `external`, or `none`. When a managed zmx
generation exists, top-level `runtime_state` always describes that generation.
Known external processes are independently reported as
`external={pids, state}`. Thus a managed `NO_CODEX` generation plus an external
`ALIVE` copy is an explicit `LIVE_MANAGED_EXTERNAL_CONFLICT` whose top-level
runtime remains `NO_CODEX`; consumers never have to infer which process
population the field describes. With no managed generation, `runtime_state`
describes the known external population, or is `ABSENT`.

`recent_at` comes only from Codex `thread/list.updatedAt` and is labelled
`recent_source=codex_history_updated`. It means that saved conversation metadata
changed. It does not mean that an agent is working, idle, finished, or focused.
Records sort by pinned status, then this timestamp, then stable name and key.

## View health

CX Deck reads the zmx process/client table once and the iTerm GUID/TTY inventory
once, then classifies each managed live generation:

| State | Meaning |
| --- | --- |
| `VERIFIED_VIEW` | Exactly one generation-token-verified zmx client TTY maps to exactly one live iTerm session; the cached receipt is compatible or absent |
| `NO_VIEW` | The managed runtime is healthy and no presentation client is attached |
| `MULTIPLE_CLIENTS` | More than one attached or token-verified client makes the preferred view ambiguous |
| `UNVERIFIED_CLIENT` | An attached client cannot be mapped uniquely through TTY to iTerm |
| `STALE_RECEIPT` | Fresh reality proves one current view, but the cached GUID/TTY receipt disagrees |
| `CHANGED_GENERATION` | Workspace presentation metadata refers to a prior live generation and the current generation has no view |
| `VIEW_UNKNOWN` | Client or iTerm inventory is unavailable; runtime health remains separately reportable |
| `NOT_APPLICABLE` | There is no live managed Codex runtime to present |

Receipts are caches. Fresh generation-token, TTY, and GUID facts win.
`cx views status` never repairs a receipt. `cx views refresh` and explicit
rebuild may commit a receipt only after the current view has again been
independently verified.

## User commands

```text
cx views status [--json] [--workspace NAME]
cx views refresh [--workspace NAME]
cx views rebuild [--workspace NAME]
cx find QUERY [--json]
cx focus --next|--previous
```

`views status` and `find` are read-only. Search is a case-insensitive substring
over display name, group, cwd, and UUID metadata. UUID prefixes are useful for
display filtering only; an ambiguous prefix returns every candidate and never
becomes identity.

Next/previous navigation includes only freshly verified views. Caller position
is proven by caller TTY through the verified client/generation chain. From an
ordinary shell, `next` selects the first and `previous` the last stable-ordered
candidate. Navigation focuses an existing GUID/TTY after a second fresh check;
it opens no client and starts no runtime.

Workspace-scoped rebuild resolves membership by exact durable identity. It
reuses verified views, creates one view only for `NO_VIEW`, and refreshes a stale
receipt after verification. It refuses multiple, unverifiable, changed, or
unknown view states and never starts a saved-only runtime. It affects only the
selected membership set. This adaptive presentation repair is distinct from
`cx workspace open NAME`, which applies an optional saved exact split topology.

## Provider degradation and non-goals

Failure to inventory iTerm yields `VIEW_UNKNOWN` and a warning while saved,
runtime, external-process, group, and workspace facts remain available. Ordinary
inventory uses the existing supported AppleScript GUID/TTY traversal, so the
optional iTerm2 Python package remains limited to exact workspace capture and
restore.

T3 does not infer task progress, search transcripts, reconnect automatically,
close clients, move panes, or resume conversations. Read-only status, search,
and navigation do not change zmx labels. `ALIVE` means only that a Codex process
exists, and `NO_VIEW` never means that Codex has stopped.

## JSON compatibility

During the unreleased v0.8 development line, `cx status --json`, dashboard JSON,
and `cx resume --json` converge on `cxdeck.inventory/v1`. This replaces the
v0.7 session-shaped status object with one conversation record per exact
identity plus explicit unbound runtime records. The schema identifier and the
separate state dimensions are the compatibility boundary for v0.8 consumers.
