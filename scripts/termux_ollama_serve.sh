#!/usr/bin/env bash
# Launch a separately installed native Termux engine; never install packages,
# download models, change PAIR configuration or stop an existing service.
set -u
mode="${1:---cpu}"
case "$mode" in
    --help|-h)
        cat <<'EOF'
Usage: bash termux_ollama_serve.sh [--cpu|--auto]
Run OUTSIDE Debian, in Termux, after installing native Termux Ollama.
--cpu (default): disable Vulkan for a CPU baseline.
--auto: allow the engine's default GPU discovery; GPU use is not guaranteed.
Serves only on 127.0.0.1:11435, in the foreground. Ctrl+C stops this engine.
Use a separate Termux session for PAIR in Debian. Configure PAIR's Ollama
engine port as 11435; leave its client-facing proxy on 11434.
Models remain in the Termux engine's store (or inherited OLLAMA_MODELS).
This launcher does not migrate the Debian model store.
EOF
        exit 0 ;;
    --cpu|--auto) ;;
    *) printf 'Unknown mode; use --help.\n' >&2; exit 2 ;;
esac
if (( $# > 1 )); then printf 'Too many arguments.\n' >&2; exit 2; fi
if [[ "${PREFIX:-}" != */com.termux/* || -r /etc/debian_version ]]; then
    printf 'Run this launcher in native Termux, outside Debian/PRoot.\n' >&2
    exit 2
fi
native_ollama="$PREFIX/bin/ollama"
if [[ ! -x "$native_ollama" ]]; then
    printf 'Native Termux Ollama is missing: %s\n' "$native_ollama" >&2
    printf 'Check its availability with pkg show ollama in Termux.\n' >&2
    exit 1
fi
# One concurrent request and one resident model limit memory pressure on a
# phone. Inherited context/model-location settings remain caller-controlled.
export OLLAMA_HOST=127.0.0.1:11435
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NO_CLOUD=1
if [[ "$mode" == --cpu ]]; then
    export OLLAMA_VULKAN=0
else
    unset OLLAMA_VULKAN
fi
printf 'Native Termux Ollama; mode=%s; endpoint=%s\n' "$mode" "$OLLAMA_HOST"
exec "$native_ollama" serve
