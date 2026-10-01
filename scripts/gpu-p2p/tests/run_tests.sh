#!/bin/bash
# P2P test stages, run INSIDE a CUDA + PyTorch container (nvcc, torchrun) on an idle node; normally
# started by run_host.sh. Stages, in order, stopping at the first failing one unless KEEP_GOING=1:
#   p2ptest      peer access matrix, integrity of peer stores/loads/copies, copy bandwidth, host<->GPU
#   ordering     peer stores + __threadfence_system() + flag (host / owner / third GPU): no stale data
#   stale        peer re-read after the owner overwrote the buffer and signalled an event: no stale data
#   hostnuma     cuMemCreate pinned host memory on every NUMA node (what NCCL's SHM transport uses)
#   nccl         torchrun nccl_allreduce.py for every GPU set x NCCL variant
#   atomics      OPT-IN (ATOMICS=1): P2P atomic attributes; concurrent atomicAdd on one peer counter (loss
#                is INFO without native P2P atomics). Any Xid while it runs fails the run_host.sh kernel-log
#                check: run it outside service validation
#   managedtest  OPT-IN (MANAGED=1): cudaMallocManaged across GPU pairs. Can fault all GPUs (Xid 31, 154;
#                reboot) on static-BAR1 GPUs with drivers that lack the UVM BAR1 fix
#   compress     OPT-IN (COMPRESS=1): peer access to compressible cuMemCreate memory. Can write to host
#                RAM at 1 TiB + offset without the RM fix: only on nodes whose RAM ends below 1 TiB.
#                STAGE compress: SKIP when no pair reached a compressible peer mapping (exit 4); FAIL (exit 3)
#                when it refuses because System RAM ends above 1 TiB and COMPRESS_ARGS lacks --above-1tib
# Ends with "OVERALL: PASS" (exit 0; "OVERALL: PASS (skipped stages: ...)" when a stage was skipped) or
# "OVERALL: FAIL (first failing stage: ...)" (exit 1).
#
# Env:
#   STAGES          subset/order of the stages above (default: p2ptest ordering stale hostnuma nccl, then
#                   atomics with ATOMICS=1, managedtest with MANAGED=1 and compress with COMPRESS=1); atomics,
#                   managedtest and compress are skipped without their opt-in variable even when listed here
#   ATOMICS=1       run atomics (diagnostic, not part of service validation)
#   MANAGED=1       run managedtest; MANAGED_ARGS its options (e.g. "--quick --pair 0,1"; see managedtest.cu)
#   COMPRESS=1      run compress (passes --allow-host-ram-risk); COMPRESS_ARGS its other options
#   HOST_RAM_END    end of host System RAM (hex), for compress; run_host.sh sets it (docker masks
#                   /sys/firmware)
#   KEEP_GOING=1    run everything despite failures (e.g. a baseline before installing P2P)
#   P2PTEST_BUF_GB  per-GPU buffer cap for p2ptest; needed when BAR1 < VRAM (run_host.sh sets it)
#   HOSTNUMA_GB     GiB to hold per NUMA node in the cumulative hostnuma check (default 8)
#   GPU_SETS        space-separated CUDA_VISIBLE_DEVICES lists (default: from the NUMA topology: all
#                   GPUs, each NUMA node's GPUs, a same-socket pair per node, two cross-socket pairs)
#   NCCL_VARIANTS   space-separated subset of: default p2p-sys no-p2p (default: "default p2p-sys")
#                     default  no NCCL settings
#                     p2p-sys  NCCL_P2P_LEVEL=SYS NCCL_LOCAL_REGISTER=0 NCCL_GRAPH_REGISTER=0; FAILS if
#                              NCCL does not use a P2P transport
#                     no-p2p   NCCL_P2P_DISABLE=1 (host-staged reference)
#   RESIDENT_GB     resident GiB per GPU during the NCCL runs (default 0; e.g. 38 on 48 GB cards)
#   SIZES_MB, ITERS passed to nccl_allreduce.py
#   CUDA_ARCH       nvcc -arch (default sm_89 = RTX 4090); BUILD_DIR (default /tmp/gpu-p2p-build)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BUILD=${BUILD_DIR:-/tmp/gpu-p2p-build}
ARCH=${CUDA_ARCH:-sm_89}
default_stages="p2ptest ordering stale hostnuma nccl"
[ "${ATOMICS:-0}" = 1 ] && default_stages+=" atomics"
[ "${MANAGED:-0}" = 1 ] && default_stages+=" managedtest"
[ "${COMPRESS:-0}" = 1 ] && default_stages+=" compress"
STAGES=${STAGES:-$default_stages}
NCCL_VARIANTS=${NCCL_VARIANTS:-default p2p-sys}
# Same GPU numbering as nvidia-smi (PCI bus order); the CUDA default orders by speed.
export CUDA_DEVICE_ORDER=PCI_BUS_ID

overall=0
first_fail=""
skipped=""
finish() {
    echo
    if [ "$overall" = 0 ]; then
        echo "OVERALL: PASS${skipped:+ (skipped stages:$skipped)}"
        exit 0
    fi
    echo "OVERALL: FAIL (first failing stage: $first_fail)"
    exit 1
}
skip() {   # stage reason
    echo "$2"
    echo "STAGE $1: SKIP"
    skipped+=" $1"
}
result() {   # stage rc
    if [ "$2" = 0 ]; then
        echo "STAGE $1: PASS"
        return
    fi
    echo "STAGE $1: FAIL"
    overall=1
    first_fail=${first_fail:-$1}
    [ "${KEEP_GOING:-0}" = 1 ] || finish
}

build() {   # name [extra nvcc args]
    local name=$1
    shift
    [ -x "$BUILD/$name" ] && [ "$BUILD/$name" -nt "$HERE/$name.cu" ] && return 0
    nvcc -O2 -arch="$ARCH" -o "$BUILD/$name" "$HERE/$name.cu" "$@"
}

# --- GPU topology -------------------------------------------------------------------------------
declare -A node_gpus
gpus=()
nodes=()
while IFS=', ' read -r idx bus; do
    [ -n "$idx" ] || continue
    dom=${bus%%:*}
    rest=${bus#*:}
    sys=$(printf '%04x:%s' "$((16#$dom))" "${rest,,}")
    numa=$(cat "/sys/bus/pci/devices/$sys/numa_node" 2>/dev/null || echo 0)
    [ "$numa" -ge 0 ] 2>/dev/null || numa=0
    gpus+=("$idx")
    [ -n "${node_gpus[$numa]:-}" ] || nodes+=("$numa")
    node_gpus[$numa]="${node_gpus[$numa]:-} $idx"
    echo "GPU$idx $sys numa $numa"
done < <(nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null)
if [ ${#gpus[@]} = 0 ]; then
    echo "nvidia-smi found no GPUs (is the container started with --gpus all?)"
    overall=1
    first_fail=setup
    finish
fi

default_gpu_sets() {
    local sets=() a b g
    local all
    all=$(IFS=,; echo "${gpus[*]}")
    sets+=("$all")
    if [ ${#nodes[@]} -gt 1 ]; then
        for n in "${nodes[@]}"; do
            read -ra g <<<"${node_gpus[$n]}"
            [ ${#g[@]} -ge 2 ] && sets+=("$(IFS=,; echo "${g[*]}")")
        done
        for n in "${nodes[@]}"; do   # same socket
            read -ra g <<<"${node_gpus[$n]}"
            [ ${#g[@]} -ge 2 ] && sets+=("${g[0]},${g[1]}")
        done
        read -ra a <<<"${node_gpus[${nodes[0]}]}"
        read -ra b <<<"${node_gpus[${nodes[1]}]}"
        sets+=("${a[0]},${b[0]}" "${a[-1]},${b[-1]}")   # cross socket
    else
        [ ${#gpus[@]} -ge 2 ] && sets+=("${gpus[0]},${gpus[1]}")
        [ ${#gpus[@]} -ge 4 ] && sets+=("${gpus[-2]},${gpus[-1]}")
    fi
    printf '%s\n' "${sets[@]}" | awk -F, 'NF >= 2 && !seen[$0]++' | tr '\n' ' '
}
GPU_SETS=${GPU_SETS:-$(default_gpu_sets)}

mkdir -p "$BUILD"

# --- stages -------------------------------------------------------------------------------------
for stage in $STAGES; do
    echo
    echo "===== $stage"
    case $stage in
    p2ptest | ordering | stale | atomics | managedtest | compress | hostnuma)
        if ! command -v nvcc >/dev/null; then
            echo "nvcc not found: use an image with the CUDA toolkit (e.g. an NGC PyTorch image)"
            result "$stage" 1
            continue
        fi
        ;;
    esac
    case $stage in
    p2ptest)
        build p2ptest || { result p2ptest 1; continue; }
        [ -n "${P2PTEST_BUF_GB:-}" ] && echo "P2PTEST_BUF_GB=$P2PTEST_BUF_GB"
        timeout 1800 "$BUILD/p2ptest"
        result p2ptest $?
        ;;
    ordering | stale | atomics)
        if [ "$stage" = atomics ] && [ "${ATOMICS:-0}" != 1 ]; then
            skip atomics "atomics is opt-in (ATOMICS=1): a diagnostic run, not part of service validation"
            continue
        fi
        build "$stage" || { result "$stage" 1; continue; }
        timeout 1800 "$BUILD/$stage"
        result "$stage" $?
        ;;
    managedtest)
        if [ "${MANAGED:-0}" != 1 ]; then
            skip managedtest "managedtest is opt-in (MANAGED=1): it can fault all GPUs on drivers without the UVM BAR1 fix"
            continue
        fi
        echo "WARNING: managed memory across GPUs. On static-BAR1 GPUs (BAR1 >= VRAM) with a driver that lacks the"
        echo "WARNING: UVM BAR1 fix this faults all GPUs (Xid 31, then Xid 154; only a reboot recovers)."
        echo "WARNING: With the fix and uvm_bar1_p2p_managed=1 (opt-in) a reboot may still be needed."
        build managedtest || { result managedtest 1; continue; }
        # shellcheck disable=SC2086
        timeout 3600 "$BUILD/managedtest" ${MANAGED_ARGS:-}
        result managedtest $?
        ;;
    compress)
        if [ "${COMPRESS:-0}" != 1 ]; then
            skip compress "compress is opt-in (COMPRESS=1): without the RM fix it can write to host RAM at 1 TiB + offset"
            continue
        fi
        build compress -lcuda || { result compress 1; continue; }
        # shellcheck disable=SC2086
        timeout 1800 "$BUILD/compress" --allow-host-ram-risk ${COMPRESS_ARGS:-}
        rc=$?
        if [ "$rc" = 4 ]; then
            skip compress "compress: no pair reached a compressible peer mapping (see the RESULT line)"
        else
            result compress $rc
        fi
        ;;
    hostnuma)
        build hostnuma -lcuda || { result hostnuma 1; continue; }
        timeout 600 "$BUILD/hostnuma" sizes
        rc=$?
        timeout 600 "$BUILD/hostnuma" cumulative "${HOSTNUMA_GB:-8}" || rc=1
        result hostnuma $rc
        ;;
    nccl)
        command -v torchrun >/dev/null || { echo "torchrun not found in this image"; result nccl 1; continue; }
        echo "GPU sets: $GPU_SETS   variants: $NCCL_VARIANTS   resident: ${RESIDENT_GB:-0} GiB/GPU"
        rc=0
        for variant in $NCCL_VARIANTS; do
            case $variant in
            default) envs="" ;;
            p2p-sys) envs="NCCL_P2P_LEVEL=SYS NCCL_LOCAL_REGISTER=0 NCCL_GRAPH_REGISTER=0" ;;
            no-p2p) envs="NCCL_P2P_DISABLE=1" ;;
            *) echo "unknown NCCL variant '$variant'"; rc=1; continue ;;
            esac
            for devs in $GPU_SETS; do
                n=$(($(printf %s "$devs" | tr -cd , | wc -c) + 1))
                echo "== [$variant] GPUs $devs${envs:+ ($envs)}"
                log=$BUILD/nccl.log
                # shellcheck disable=SC2086
                env $envs CUDA_VISIBLE_DEVICES="$devs" NCCL_DEBUG=INFO timeout "${TIMEOUT_NCCL:-600}" \
                    torchrun --standalone --nproc_per_node="$n" "$HERE/nccl_allreduce.py" >"$log" 2>&1
                r=$?
                grep -aE '^(all_reduce|resident|RESULT)' "$log" | sed 's/^/   /'
                echo "   transports: $(grep -ao 'via [A-Za-z0-9/_]*' "$log" | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
                bad=0
                if [ "$r" != 0 ] || ! grep -aq '^RESULT: PASS' "$log"; then
                    echo "   FAILED rc=$r: $(grep -a -m3 -E 'Error|error|FAILED|Timeout|unhandled' "$log" | cut -c1-200 | tr '\n' ' ')"
                    bad=1
                elif [ "$variant" = p2p-sys ] && ! grep -aq 'via P2P' "$log"; then
                    echo "   FAILED: NCCL did not use a P2P transport with NCCL_P2P_LEVEL=SYS"
                    bad=1
                fi
                if [ "$bad" = 1 ]; then
                    rc=1
                    [ "${KEEP_GOING:-0}" = 1 ] || break 2
                fi
            done
        done
        result nccl $rc
        ;;
    *)
        echo "unknown stage '$stage'"
        result "$stage" 1
        ;;
    esac
done
finish
