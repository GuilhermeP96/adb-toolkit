#!/usr/bin/env bash
# Run in Termux. Saves separate inventories; never installs dependencies.
set -u
if [[ "${1:-}" == --help ]]; then
    printf 'Usage: bash android_perf_collect.sh\nRun in Termux; writes inventories next to this script.\n'
    exit 0
fi
if (( $# != 0 )); then
    printf 'Unexpected argument; use --help.\n' >&2
    exit 2
fi
if [[ "${PREFIX:-}" != */com.termux/* ]]; then
    printf 'Run this collector in Termux, outside Debian.\n' >&2
    exit 2
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
probe="$script_dir/android_perf_probe.sh"
[[ -r "$probe" ]] || { printf 'Missing probe: %s\n' "$probe" >&2; exit 1; }
result_dir=$(mktemp -d "$script_dir/results-XXXXXX") || exit 1
printf 'Output: %s\n' "$result_dir"
bash --noprofile --norc "$probe" --graphics > "$result_dir/termux.txt" 2>&1 || exit 1
if ! command -v proot-distro >/dev/null; then
    printf 'proot-distro is unavailable; Termux inventory only.\n' >&2
    exit 1
fi
# Send the same script via stdin, avoiding assumptions about storage bindings
# or the old installed-rootfs versus new containers directory layout.
# Explicit bash options avoid starting services from Debian shell profiles.
if proot-distro login debian --shared-tmp -- /bin/bash --noprofile --norc -s -- --graphics \
    < "$probe" > "$result_dir/debian.txt" 2>&1; then
    printf 'Collected Termux and Debian. Read termux.txt and debian.txt in %s\n' "$result_dir"
else
    printf 'Debian collection failed; see %s/debian.txt\n' "$result_dir" >&2
    exit 1
fi
