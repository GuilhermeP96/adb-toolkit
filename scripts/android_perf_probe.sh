#!/usr/bin/env bash
# Read-only inventory: run in Termux and again inside the Debian session.
# Does not install packages, change governors, start services or run workloads.
set -u

usage() {
    cat <<'EOF'
Usage: bash android_perf_probe.sh [--graphics]
       bash android_perf_probe.sh --help

Run once in Termux and once inside Debian to compare architecture, CPU
access, GPU device permissions and installed graphics components.
--graphics additionally queries the existing GL/Vulkan stack (20s timeout
per tool). It does not start an X server; GL needs an existing DISPLAY.
Graphics drivers may initialize devices or write their normal caches.
Missing tools and permission denials are reported, not repaired.
Do not interpret a visible GPU device or installed driver as proof of
hardware acceleration. Confirm the renderer and the actual application.
EOF
}

graphics=0
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --graphics) graphics=1 ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi

section() { printf '\n[%s]\n' "$1"; }
read_file() {
    if [[ -r "$1" ]]; then
        printf '%s: ' "$1"
        cat -- "$1"
        printf '\n'
    else
        printf '%s: unavailable or permission denied\n' "$1"
    fi
}

section environment
if [[ "${PREFIX:-}" == */com.termux/* ]]; then
    printf 'Context: Termux (check libc and architecture below)\n'
else
    printf 'Context: distro/other; UID 0 alone does not prove kernel root\n'
fi
uname -srm
id
[[ ! -r /etc/os-release ]] || sed -n '/^PRETTY_NAME=/p' /etc/os-release
command -v getconf >/dev/null && getconf GNU_LIBC_VERSION 2>/dev/null
command -v dpkg >/dev/null && dpkg --print-architecture 2>/dev/null
if command -v getprop >/dev/null; then
    for prop in ro.product.model ro.soc.manufacturer ro.soc.model ro.board.platform ro.build.version.release ro.product.cpu.abilist; do
        printf '%s: %s\n' "$prop" "$(getprop "$prop" 2>/dev/null)"
    done
fi

section cpu
read_file /sys/devices/system/cpu/online
[[ ! -r /proc/self/status ]] || sed -n '/^Cpus_allowed_list:/p; /^Mems_allowed_list:/p; /^CapEff:/p; /^Seccomp:/p' /proc/self/status
[[ ! -r /proc/cpuinfo ]] || awk '/^(Features|Hardware|model name)[[:space:]]*:/ { if (!seen[$0]++) print }' /proc/cpuinfo
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [[ -d "$policy" ]] || continue
    for field in affected_cpus cpuinfo_max_freq scaling_max_freq scaling_cur_freq scaling_governor; do
        read_file "$policy/$field"
    done
done

section memory
[[ ! -r /proc/meminfo ]] || sed -n '/^MemTotal:/p; /^MemAvailable:/p; /^SwapTotal:/p; /^SwapFree:/p' /proc/meminfo

section gpu_devices
for device in /dev/kgsl-3d0 /dev/mali0 /dev/mali /dev/dri/renderD*; do
    [[ -e "$device" ]] || continue
    ls -l -- "$device"
    printf 'Readable=%s Writable=%s (opening/ioctl access is not tested)\n' \
        "$([[ -r "$device" ]] && echo yes || echo no)" \
        "$([[ -w "$device" ]] && echo yes || echo no)"
done

section components
for tool in proot proot-distro qemu-aarch64 qemu-x86_64 termux-x11 virgl_test_server_android virgl_test_server glxinfo eglinfo vulkaninfo nvpair nvpair-tui ollama llama-server nmap; do
    command -v "$tool" || printf '%s: not in PATH\n' "$tool"
done
if command -v dpkg-query >/dev/null; then
    dpkg-query -W -f='${binary:Package} ${Version} ${db:Status-Status}\n' 2>/dev/null |
        awk '$1 ~ /^(proot|mesa|libgl|libegl|libvulkan|vulkan|virgl|angle|termux-x11|ollama|llama|nvpair)/ && $3 == "installed"'
fi
printf 'DISPLAY=%s\nGALLIUM_DRIVER=%s\nMESA_LOADER_DRIVER_OVERRIDE=%s\n' \
    "${DISPLAY:-unset}" "${GALLIUM_DRIVER:-unset}" "${MESA_LOADER_DRIVER_OVERRIDE:-unset}"

section ollama_endpoints
if command -v curl >/dev/null; then
    # PAIR may own 11434 and move the local engine to 11435 or a higher port.
    # Only version metadata is queried; no model is loaded and no inference runs.
    for port in 11434 11435; do
        printf '127.0.0.1:%s/api/version: ' "$port"
        curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
            "http://127.0.0.1:$port/api/version" 2>/dev/null || printf 'unavailable'
        printf '\n'
    done
else
    printf 'curl unavailable; endpoint checks skipped\n'
fi

if (( graphics )); then
    section graphics_queries
    if ! command -v timeout >/dev/null; then
        printf 'Skipped: timeout command is unavailable\n'
    else
        if [[ -n "${DISPLAY:-}" ]] && command -v glxinfo >/dev/null; then
            timeout 20s glxinfo -B 2>&1 || printf 'glxinfo failed or timed out\n'
        else
            printf 'glxinfo skipped: missing tool or DISPLAY\n'
        fi
        if command -v vulkaninfo >/dev/null; then
            timeout 20s vulkaninfo --summary 2>&1 || printf 'vulkaninfo failed or timed out\n'
        else
            printf 'vulkaninfo is unavailable\n'
        fi
    fi
fi

exit 0
