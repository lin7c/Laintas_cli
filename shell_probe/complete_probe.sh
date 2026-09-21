#!/bin/bash
# Probe a command's system bash-completion definition and print COMPREPLY.
# Args: <word0> <word1> ... <cur>   (fully-typed words, then the fragment).
# Prints one candidate per line. Prints nothing (exit 0) when the command has
# no completion definition. Sources ONLY the system bash_completion, never the
# user's rc/profile (the caller runs bash --norc --noprofile).
set +e
BC=/usr/share/bash-completion/bash_completion
DIR=/usr/share/bash-completion/completions
[ -r "$BC" ] || exit 0
source "$BC" >/dev/null 2>&1

args=("$@")
n=${#args[@]}
[ "$n" -lt 1 ] && exit 0
cmd=${args[0]}
cur=${args[$((n-1))]}
prev=$([ "$n" -ge 2 ] && printf '%s' "${args[$((n-2))]}")

# Register the command's definition: lazy loader first, explicit source second.
type _completion_loader >/dev/null 2>&1 && _completion_loader "$cmd" >/dev/null 2>&1
complete -p "$cmd" >/dev/null 2>&1 || { [ -r "$DIR/$cmd" ] && source "$DIR/$cmd" >/dev/null 2>&1; }

f=$(complete -p "$cmd" 2>/dev/null | sed -n 's/.*-F[[:space:]]\{1,\}\([^[:space:]]*\).*/\1/p' | head -1)
[ -z "$f" ] && exit 0

# bash calls a completion function as: func "$cmd" "$cur" "$prev". Passing only
# "$cur" "$prev" (dropping $1) left git's wrapper without its command word, so
# it returned git's top-level verbs for every context. With the correct $1 the
# stock __git_wrap__git_main works; no unwrapping needed.
COMP_WORDS=("${args[@]}")
COMP_CWORD=$((n-1))
COMP_LINE="${args[*]} "
COMP_POINT=${#COMP_LINE}
COMPREPLY=()
"$f" "$cmd" "$cur" "$prev" >/dev/null 2>&1
for i in "${COMPREPLY[@]}"; do printf '%s\n' "$i"; done
