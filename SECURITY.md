# Security policy

Report suspected vulnerabilities through
[GitHub Private Vulnerability Reporting](https://github.com/lrgthu/cxdeck/security/advisories/new).
Do not open a public issue for credential exposure, session hijacking, arbitrary
command execution, identity confusion, or destructive runtime behavior. Never
include credentials, transcripts, private terminal output, or private
conversation data in a public report.

Security fixes target the latest release branch. CX Deck treats exact Codex UUID
identity, zmx generation verification, process ownership, private state-file
permissions, and symlink-safe writes as security boundaries. A presentation
failure must never cause a process restart or duplicate conversation.
