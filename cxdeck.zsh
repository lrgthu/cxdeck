# CX Deck shell integration. Interactive Codex runs become managed sessions;
# utility commands continue directly to the Codex CLI.
for _cx_alias in cx cxl cxls cxstatus cxa cxt cxd cxkill cxkt cxinfo cxhelp; do
  unalias "$_cx_alias" 2>/dev/null || true
done
unset _cx_alias
typeset -g _CX_MODULE_DIR="${${(%):-%N}:A:h}"
_cx_run() {
  emulate -L zsh
  if ! command -v python3 >/dev/null 2>&1; then
    print -u2 -- 'Python 3.9+ is required. On macOS: brew install python'
    return 1
  fi
  command python3 "$_CX_MODULE_DIR/console_entry.py" "$@"
}
cx() {
  if [[ "${1:-}" == doctor ]]; then
    if [[ -n "${_CX_CODEX_WRAPPER_BODY:-}" && "${functions[codex]:-}" == "$_CX_CODEX_WRAPPER_BODY" && "${CX_WRAP_CODEX:-1}" != 0 ]]; then
      print -- 'codex wrapper: installed (interactive, unmanaged terminal calls)'
    else
      print -- 'codex wrapper: bypassed or an existing alias/function was preserved; use cx directly'
    fi
  fi
  _cx_run "$@"
}
cxl()      { _cx_run status "$@"; }
cxls()     { _cx_run status "$@"; }
cxstatus() { _cx_run status "$@"; }
cxa()      { _cx_run attach "$@"; }
cxt()      { _cx_run task "$@"; }
cxd()      { _cx_run detach "$@"; }
cxkill()   { _cx_run kill "$@"; }
cxkt()     { _cx_run kill-task "$@"; }
cxinfo()   { _cx_run info "$@"; }
cxhelp()   { _cx_run help; }

# Do not silently replace a user's existing alias/function (it may set a profile
# or approval policy). Re-sourcing our own wrapper remains safe and idempotent.
if (( ${+aliases[codex]} )) || { (( ${+functions[codex]} )) && [[ "${functions[codex]}" != "${_CX_CODEX_WRAPPER_BODY:-}" ]]; }; then
  print -u2 -- '[cx] Existing codex alias/function preserved. Use cx resume or cx; inspect whence -v codex.'
else
  function codex {
    emulate -L zsh
    if [[ "${CX_WRAP_CODEX:-1}" == 0 || -n "${CX_MANAGED:-}" || ! -t 0 || ! -t 1 ]]; then
      command codex "$@"
    else
      _cx_run run -- "$@"
    fi
  }
  typeset -g _CX_CODEX_WRAPPER_BODY="${functions[codex]}"
fi
