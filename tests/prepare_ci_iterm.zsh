#!/bin/zsh
# Isolated CI only: prepare the official, verified app for unattended first launch.
# Never modify TCC/Automation permissions or global Gatekeeper policy.
set -eu
[[ "${GITHUB_ACTIONS:-}" == true && "$(uname -s)" == Darwin ]] || {
  print -u2 'This helper is restricted to the disposable macOS GitHub runner.'
  exit 1
}
app=/Applications/iTerm.app
[[ -d "$app" && ! -L "$app" ]] || exit 1
/usr/bin/codesign --verify --deep --strict --verbose=2 "$app"
/usr/sbin/spctl --assess --type execute --verbose=2 "$app"
# Only after both signature and system-policy assessment succeed, acknowledge
# this specific freshly downloaded test application for noninteractive launch.
if /usr/bin/xattr -p com.apple.quarantine "$app" >/dev/null 2>&1; then
  /usr/bin/xattr -dr com.apple.quarantine "$app"
fi
