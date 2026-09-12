# CX Deck zmx runtime contract

cx requires zmx 0.8.1 or newer. Installation and updates are explicit:

```zsh
brew install neurosnap/tap/zmx
```

`zmx version` supplies the version and socket directory. `zmx list` supplies
the exact session name, daemon PID, client count, creation time, cwd, command,
and labels. cx rejects incomplete, duplicate, or malformed generation fields.

Detached creation uses:

```text
zmx attach --labels "k=v ..." SESSION COMMAND ARG...
```

The initial client uses `/dev/null`; zmx retains the interactive PTY and child
until a later attach. This preserves Codex stdin and full-screen TUI behavior.
Every cx-managed client receives `ZMX_NO_DETACH_KEY=1`.

Managed labels are:

```text
cx_managed=1
cx_version=<launching cx version>
cx_zmx_version=<launching zmx version>
cx_thread_id=<exact UUID when known>
cx_codex_home=<encoded realpath>
cx_launch_policy=yolo|safe
cx_launch_mode=new|resume|fork
cx_launch_cwd=<encoded realpath>
```

zmx label syntax requires encoded filesystem paths. cx uses a strict `b64_`
base64url representation and validates decoded paths as absolute paths.

The provider surface is limited to version/preflight, list, attach/create,
detach, labels, and explicit kill. Normal cx operation does not call zmx
`send`, `print`, `tail`, `history`, or `write`.
