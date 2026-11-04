let zshCompletion = #"""
# Enable MyBox Tab completion with: eval "$(mybox completion zsh)"
if (( ! $+functions[compdef] )); then
    autoload -Uz compinit
    compinit
fi

_mybox_box_names() {
    local -a names candidates
    local name query="${PREFIX}${SUFFIX}"
    names=("${(@f)$(command "${words[1]}" _complete-boxes 2>/dev/null)}")
    for name in "${names[@]}"; do
        if [[ -n "$name" && "$name" == *"$query"* ]]; then
            candidates+=("$name")
        fi
    done
    (( ${#candidates} )) && compadd -U -- "${candidates[@]}"
}

_mybox() {
    local -a commands
    commands=(
        'create:Create a box'
        'enter:Open a shell or run a Linux command'
        'copy:Copy a box'
        'import:Copy files into a box'
        'export:Copy files from a box'
        'start:Start a box'
        'stop:Stop a box'
        'ports:Manage port mappings'
        'list:List boxes'
        'setup-network:Install the firewall helper'
        'completion:Print a shell completion script'
        'help:Show help'
        '--version:Show the version'
    )
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        return
    fi
    case "${words[2]}" in
        enter|start|stop|copy|import|export)
            if (( CURRENT == 3 )); then
                _mybox_box_names
            elif [[ "${words[2]}" == import ]] && (( CURRENT == 4 )); then
                _files
            elif [[ "${words[2]}" == export ]] && (( CURRENT == 5 )); then
                _files
            elif [[ "${words[2]}" == enter ]] && (( CURRENT == 4 )); then
                compadd -- --
            fi
            ;;
        create)
            if (( CURRENT >= 4 )); then
                case "${words[CURRENT-1]}" in
                    --inside-port|--outside-port|--cpus|--memory) return ;;
                esac
                compadd -- --inside-port --outside-port --cpus --memory
            fi
            ;;
        ports)
            if (( CURRENT == 3 )); then
                compadd -- add remove list
            elif (( CURRENT == 4 )) && [[ "${words[3]}" == (add|remove|list) ]]; then
                _mybox_box_names
            elif (( CURRENT >= 5 )); then
                case "${words[CURRENT-1]}" in
                    --inside-port|--outside-port) return ;;
                esac
                case "${words[3]}" in
                    add) compadd -- --inside-port --outside-port ;;
                    remove) compadd -- --outside-port ;;
                esac
            fi
            ;;
        completion)
            (( CURRENT == 3 )) && compadd -- zsh
            ;;
    esac
}

compdef _mybox mybox
"""#
