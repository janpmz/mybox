# Shared by the data-preserving cleanup script and its process-shutdown tests.
# Sourcing this file does not change services or files.

mybox_runtime_processes() {
    # comm is the executable path; command would also match shell arguments,
    # editor commands, and diagnostic text that happens to mention the runtime.
    /bin/ps -axww -o pid= -o comm= | /usr/bin/awk -v root="$1/" '
        { pid=$1; sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "");
          if (index($0, root) == 1) print pid " " $0 }
    '
}

mybox_wait_for_runtime_exit() {
    local runtime_root=$1
    local attempts=${2:-50}
    local remaining attempt
    for ((attempt=0; attempt<attempts; attempt++)); do
        remaining=$(mybox_runtime_processes "$runtime_root") || return 2
        [ -n "$remaining" ] || return 0
        /bin/sleep 0.2
    done
    remaining=$(mybox_runtime_processes "$runtime_root") || return 2
    [ -n "$remaining" ] || return 0
    printf '%s\n' "$remaining" >&2
    return 1
}
