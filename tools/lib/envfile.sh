# shellcheck shell=bash
# Safe reader for systemd-style EnvironmentFile= files. Source it, do not execute it.
#
# systemd does no expansion and runs nothing: `a$b`, `p(ss` and backticks are literal
# characters in a value. Sourcing such a file as shell breaks on or executes them, so every
# script that reads a unit's env file goes through here instead.
#
#   envfile_load FILE        export every valid KEY=VALUE of FILE (later lines win)
#   envfile_get  FILE KEY    print the last value of KEY in FILE (nothing if unset)
#
# Rules: blank lines and lines starting with # or ; are skipped; KEY must match
# [A-Za-z_][A-Za-z0-9_]*; whitespace around the line and the KEY is trimmed; one pair of
# matching surrounding quotes is stripped from the value; nothing is expanded or executed.
# A missing or unreadable file is not an error (return 0), like EnvironmentFile=-.

# Echo "KEY<TAB>VALUE" pairs, one per valid line.
_envfile_parse() {
    local file="$1" line key val
    [[ -r "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" || "$line" == \#* || "$line" == \;* ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        val="${val#"${val%%[![:space:]]*}"}"
        if [[ ${#val} -ge 2 && ( "${val:0:1}" == '"' || "${val:0:1}" == "'" ) \
              && "${val: -1}" == "${val:0:1}" ]]; then
            val="${val:1:${#val}-2}"
        fi
        printf '%s\t%s\n' "$key" "$val"
    done <"$file"
}

envfile_load() {
    local key val
    while IFS=$'\t' read -r key val; do
        export "$key=$val"
    done < <(_envfile_parse "$1")
}

envfile_get() {
    local want="$2" key val out=""
    while IFS=$'\t' read -r key val; do
        [[ "$key" == "$want" ]] && out="$val"
    done < <(_envfile_parse "$1")
    printf '%s' "$out"
}
