# Security policy

Report a suspected vulnerability privately through GitHub's security advisory
feature for this repository. Do not include credentials, transcripts, or private
conversation data in a public issue.

Security fixes target the latest release branch. CX Deck treats exact Codex UUID
identity, zmx generation verification, process ownership, private state-file
permissions, and symlink-safe writes as security boundaries. A presentation
failure must never cause a process restart or duplicate conversation.
