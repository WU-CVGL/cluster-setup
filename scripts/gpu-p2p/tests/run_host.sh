#!/bin/bash
# Runs run_tests.sh in a CUDA + PyTorch container on this node and writes <label>.log.
# usage: ./run_host.sh <image> [label]
#   image  an image with the CUDA toolkit (nvcc) and PyTorch (torchrun), e.g. an NGC PyTorch image
#   label  log name (default p2p-test-<timestamp>); the log goes to $OUT_DIR (default: current directory)
# The node must be idle: the script refuses while GPUs run compute processes (FORCE=1 overrides).
# GPUS: comma-separated GPU indices or UUIDs to test (default: all). Only these GPUs are given to the
#   container (docker --gpus device=...) and queried for the idle check, BAR1 and the link sampling; use it
#   to leave out a faulty GPU.
# Needs docker access, not root. Besides the container output the log has: driver, kernel command line,
# BAR1, nvidia-smi topo, the UVM managed-memory mode; the PCIe link (gen/width) sampled every 500 ms during
# the run with the maximum seen per GPU; and kernel-log lines (Xid, METHOD3, asserts, nvidia-uvm) since the
# start (adm/systemd-journal group).
# Passed into the container when set: STAGES ATOMICS MANAGED MANAGED_ARGS COMPRESS COMPRESS_ARGS HOST_RAM_END
#   KEEP_GOING P2PTEST_BUF_GB HOSTNUMA_GB GPU_SETS NCCL_VARIANTS RESIDENT_GB SIZES_MB ITERS CUDA_ARCH TIMEOUT_NCCL
#   (see run_tests.sh). P2PTEST_BUF_GB defaults to 2 when a GPU's BAR1 is resized (> 256 MiB) but smaller
#   than its VRAM (legacy peer access must fit into BAR1).
# MANAGED=1 (opt-in managedtest stage): the log names the UVM mode from the nvidia_uvm parameter
#   uvm_bar1_p2p_managed (fixed driver) and BAR1 vs VRAM. The script refuses when the parameter is missing
#   and BAR1 >= VRAM (static BAR1 without the UVM fix: the test faults all GPUs); MANAGED_FORCE=1 overrides.
#   The opt-in (uvm_bar1_p2p_managed=1) only affects static-BAR1 pairs with a 2MB-aligned DMA window; the
#   kernel-log summary notes pairs whose window is not aligned (they stage through host memory).
# HOST_RAM_END: the end of System RAM from the host's /sys/firmware/memmap (docker masks it in the
#   container) is passed to the compress stage, which refuses when RAM extends above 1 TiB.
# The container runs with --init; after TIMEOUT (default 10800 s) it is stopped (SIGTERM, SIGKILL 60 s later)
# and removed.
# Exit status: 0 when the tests pass and the kernel log has no Xid/assert, else 1.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=${1:-}
[ -n "$IMAGE" ] || { sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 1; }
LABEL=${2:-p2p-test-$(date +%Y%m%d-%H%M%S)}
OUT_DIR=${OUT_DIR:-$PWD}
LOG=$OUT_DIR/$LABEL.log
PASS_VARS=(STAGES ATOMICS MANAGED MANAGED_ARGS COMPRESS COMPRESS_ARGS HOST_RAM_END KEEP_GOING P2PTEST_BUF_GB HOSTNUMA_GB GPU_SETS NCCL_VARIANTS RESIDENT_GB SIZES_MB ITERS CUDA_ARCH TIMEOUT_NCCL)

command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found"; exit 1; }
sel=()
gpus_arg=all
if [ -n "${GPUS:-}" ]; then
    sel=(-i "$GPUS")
    gpus_arg="\"device=$GPUS\""
fi
command -v docker >/dev/null || { echo "docker not found"; exit 1; }

apps=$(nvidia-smi "${sel[@]}" --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)
if [ -n "$apps" ] && [ "${FORCE:-0}" != 1 ]; then
    echo "GPUs are in use; the tests need an idle node (disable it in the scheduler first, FORCE=1 overrides):"
    echo "$apps"
    exit 1
fi

bar=$(nvidia-smi "${sel[@]}" -q -d MEMORY | awk '
    /^GPU / {gpu = $2}
    /FB Memory Usage/ {sec = "fb"} /BAR1 Memory Usage/ {sec = "bar1"}
    /^ *Total *:/ && sec != "" {v[sec] = $3; if (sec == "bar1") print gpu, v["fb"], v["bar1"]; sec = ""}')
if [ -z "${P2PTEST_BUF_GB:-}" ] && echo "$bar" | awk '$3 > 256 && $3 < $2 {f = 1} END {exit !f}'; then
    export P2PTEST_BUF_GB=2
fi

# UVM BAR1 managed-memory mode: the parameter of the fixed driver (any uvm_bar1_p2p* name), else absent.
# Before the first CUDA use nvidia_uvm may not be loaded: then the installed module and modprobe.d decide.
if [ -d /sys/module/nvidia_uvm/parameters ]; then
    uvm_params=$(for f in /sys/module/nvidia_uvm/parameters/uvm_bar1_p2p*; do
        [ -r "$f" ] && printf '%s=%s ' "${f##*/}" "$(cat "$f")"
    done)
else
    uvm_params=$({ modinfo -F parm nvidia_uvm || modinfo -F parm nvidia-uvm; } 2>/dev/null | sed -n 's/^\(uvm_bar1_p2p[a-z0-9_]*\):.*/\1/p' | head -1)
    if [ -n "$uvm_params" ]; then
        conf=$(modprobe -c 2>/dev/null | grep -E "^options nvidia[-_]uvm " | grep -o "$uvm_params=[0-9]*" | tail -1)
        uvm_params="${conf:-$uvm_params=0} (nvidia_uvm not loaded yet: installed module, modprobe.d) "
    fi
fi
uvm_managed=$(printf '%s' "$uvm_params" | grep -o 'uvm_bar1_p2p_managed=[0-9]*' | cut -d= -f2)
[ -n "$uvm_managed" ] || uvm_managed=$(printf '%s' "$uvm_params" | sed -n 's/^[^=]*=\([0-9]*\).*/\1/p')
static_bar1=$(echo "$bar" | awk '$3 >= $2 && $2 > 0 {f = 1} END {print f + 0}')
# BAR1P2P: dynamic BAR1 pairs (BAR1 < VRAM) and static pairs with a DMA window that is not 2MB aligned
# never use BAR1 peer mappings for managed memory, whatever uvm_bar1_p2p_managed says.
if [ -n "$uvm_params" ] && [ "$uvm_managed" = 0 ]; then
    # BAR1P2P: this reflects the parameter value only. A managedtest PASS cannot tell staging through host
    # memory from a direct BAR1 peer mapping (both give correct data); that the gate holds rests on review
    # of the driver predicate uvm_parent_gpus_bar1_managed_unsupported().
    managed_mode="gate on ($uvm_params, from the parameter value): managed pages of BAR1 peers stage through host memory"
elif [ -n "$uvm_params" ] && [ "$static_bar1" = 1 ]; then
    managed_mode="OPT-IN window ($uvm_params): managed memory uses BAR1 peer mappings (pairs whose DMA window is not 2MB aligned still stage through host memory, see the kernel log); a fault can take down all GPUs (reboot)"
elif [ -n "$uvm_params" ]; then
    managed_mode="opt-in set ($uvm_params) but BAR1 < VRAM (dynamic BAR1): the driver never uses BAR1 peer mappings for managed memory on dynamic pairs; managed pages stage through host memory, the opt-in path is NOT exercised"
elif [ "$static_bar1" = 1 ]; then
    managed_mode="NO UVM BAR1 fix on static-BAR1 GPUs: managed memory across GPUs faults all GPUs (Xid 31, Xid 154; reboot)"
else
    managed_mode="no uvm_bar1_p2p parameter, BAR1 < VRAM (dynamic BAR1): without this fork's dynamic-pair UVM guard, managed memory across GPUs addresses host RAM"
fi
managed_stage=1
case " ${STAGES:-managedtest} " in *" managedtest "*) ;; *) managed_stage=0 ;; esac
# Without the uvm_bar1_p2p parameter the modules may lack the UVM BAR1 fix: refuse on static BAR1 (faults
# all GPUs) and on dynamic BAR1 too (duanyll's Method 3 without the dynamic-pair guard addresses host RAM).
if [ "${MANAGED:-0}" = 1 ] && [ "$managed_stage" = 1 ] && [ -z "$uvm_params" ] &&
    [ "${MANAGED_FORCE:-0}" != 1 ]; then
    echo "MANAGED=1 refused: $managed_mode. Install the fixed modules first (MANAGED_FORCE=1 overrides)."
    exit 1
fi

# End (exclusive, hex) of System RAM from the firmware memory map, for the compress stage.
if [ -z "${HOST_RAM_END:-}" ]; then
    ram_end=0
    for d in /sys/firmware/memmap/*/; do
        [ "$(cat "$d/type" 2>/dev/null)" = "System RAM" ] || continue
        e=$(($(cat "$d/end") + 1))
        [ "$e" -gt "$ram_end" ] && ram_end=$e
    done
    [ "$ram_end" -gt 0 ] && HOST_RAM_END=$(printf '0x%x' "$ram_end") && export HOST_RAM_END
fi

mkdir -p "$OUT_DIR"
since=$(date '+%Y-%m-%d %H:%M:%S')
{
    echo "### $LABEL  $(date -Is)  image $IMAGE"
    echo "driver: $(head -1 /proc/driver/nvidia/version 2>/dev/null | tr -s ' ' | cut -c1-100)"
    echo "cmdline: $(cat /proc/cmdline)"
    echo "BAR1 / VRAM (MiB): $(echo "$bar" | awk '{c[$3 " / " $2]++} END {for (x in c) printf "%s x%d  ", x, c[x]}')"
    [ -n "${P2PTEST_BUF_GB:-}" ] && echo "P2PTEST_BUF_GB=$P2PTEST_BUF_GB"
    echo "UVM managed memory on BAR1 peers: $managed_mode"
    if [ "${MANAGED:-0}" = 1 ]; then
        echo "MANAGED=1: the managedtest stage runs${MANAGED_ARGS:+ with $MANAGED_ARGS}"
    else
        echo "MANAGED unset: managedtest skipped (opt-in)"
    fi
    [ "${ATOMICS:-0}" = 1 ] && echo "ATOMICS=1: the atomics stage runs (diagnostic, not a service run)"
    [ "${COMPRESS:-0}" = 1 ] &&
        echo "COMPRESS=1: the compress stage runs (host-RAM hazard without the RM fix); System RAM ends at ${HOST_RAM_END:-unknown}"
    nvidia-smi "${sel[@]}" --query-gpu=index,pci.bus_id,name --format=csv,noheader
    nvidia-smi topo -p2p r | sed -n '/^Legend/q;p'
    nvidia-smi topo -m | sed -n '/^Legend/q;p'
} >"$LOG" 2>&1
cat "$LOG"

links=$(mktemp)
nvidia-smi "${sel[@]}" --query-gpu=index,pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,pcie.link.gen.hostmax,pcie.link.width.max \
    --format=csv,noheader -lms 500 >"$links" 2>/dev/null &
sampler=$!

envargs=()
for v in "${PASS_VARS[@]}"; do
    [ -n "${!v:-}" ] && envargs+=(-e "$v=${!v}")
done
# --init: bash is not PID 1, so the SIGTERM that timeout sends (proxied by the docker CLI) stops the tests;
# -k and the final docker rm -f make sure the container is gone after a TIMEOUT.
name=gpu-p2p-test-$$
timeout -k 60 "${TIMEOUT:-10800}" docker run --init --name "$name" --rm --gpus "$gpus_arg" --ipc=host --ulimit memlock=-1 \
    --ulimit stack=67108864 --entrypoint bash -v "$HERE:/w:ro" "${envargs[@]}" "$IMAGE" /w/run_tests.sh 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
docker rm -f "$name" >/dev/null 2>&1 || true
kill "$sampler" 2>/dev/null
wait "$sampler" 2>/dev/null

post=$(mktemp)
{
    echo
    echo "=== PCIe link, maximum seen during the run (gen x width | gen max, host gen max, width max)"
    sort -t, -k1,1n -k2,2nr -k3,3nr "$links" | awk -F', *' '!seen[$1]++ {
        printf "  GPU%s: gen%s x%s | gen max %s, host max %s, width max x%s%s\n", $1, $2, $3, $4, $5, $6,
            ($3 < $6 ? "   <-- narrower than the maximum" : "")}'
    echo "=== kernel log since $since (Xid, METHOD3, asserts, NVRM, nvidia-uvm)"
    if klog=$(journalctl -k --since "$since" --no-pager -q 2>/dev/null) && [ -n "$klog" ]; then
        echo "$klog" | grep -E 'NVRM|Xid|METHOD3|Assert failed|nvidia-uvm' | cut -c1-220 | sort | uniq -c | sort -rn | head -30
        # BAR1P2P: gating a misaligned pair is the intended safe behaviour, not a failure.
        if echo "$klog" | grep -qE 'not 2MB aligned .*managed memory will not use direct peer access'; then
            echo "NOTE: a static BAR1 DMA window is not 2MB aligned: managed memory for that pair staged through host"
            echo "NOTE: memory (opt-in NOT exercised for it)"
        fi
        if echo "$klog" | grep -qE 'NVRM: Xid|Assert failed'; then
            echo "KERNEL LOG: FAIL (Xid or assert)"
            rc=1
        else
            echo "KERNEL LOG: PASS (no Xid or assert)"
        fi
    else
        echo "  kernel log not readable (join the adm or systemd-journal group); check 'sudo dmesg' by hand"
    fi
} >"$post" 2>&1
tee -a "$LOG" <"$post"
rm -f "$links" "$post"
echo "log: $LOG"
[ "$rc" = 0 ] && exit 0
exit 1
