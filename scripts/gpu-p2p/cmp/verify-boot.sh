#!/bin/bash
# Boot checks on a CMP 170HX node after a cold power cycle into a cmpunlocker build with BAR1 P2P.
# Read-only, no root (the kernel log needs the adm or systemd-journal group).
# usage: ./verify-boot.sh <number of GPUs> [<sha256 of nvidia.ko>]
#   number of GPUs  CMP 170HX cards in the node; fewer after the boot is a HARD-STOP
#   sha256          expected hash of the nvidia.ko that modprobe resolves (trees.sh show); srcversion does not
#                   tell cmpunlocker builds apart
# ECC=1 for a build with the ECC patches (nvidia-smi then reports ECC Enabled); default 0 (install.sh --no-ecc).
# What every GPU must show: DEV (PCI device ID, default 20c2), MEM_MIB and BAR1_MIB (default 65536),
# LINK_GEN (default 2) and LINK_WIDTH (default 16).
# Prints PASS / FAIL / HARD-STOP / INFO lines and "RESULT rc=<n>".
# Exit 0: all pass; 1: a FAIL (investigate, keep workloads off); 2: a HARD-STOP (roll back).
set -u
N=${1:?usage: verify-boot.sh <number of GPUs> [<sha256 of nvidia.ko>]}
SHA=${2:-}
ECC=${ECC:-0}
DEV=${DEV:-20c2}
MEM_MIB=${MEM_MIB:-65536}
BAR1_MIB=${BAR1_MIB:-65536}
LINK_GEN=${LINK_GEN:-2}
LINK_WIDTH=${LINK_WIDTH:-16}
rc=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; if [ "$rc" -lt 1 ]; then rc=1; fi; }
hard() { echo "HARD-STOP: $*"; rc=2; }
K=$(journalctl -k -b --no-pager -o cat 2>/dev/null)
[ -n "$K" ] || { echo "HARD-STOP: cannot read the kernel log (adm or systemd-journal group?)"; exit 2; }
cnt() { grep -c -E -- "$1" <<<"$K"; }

# PCI: every GPU enumerated with the 64 GB BAR1 programmed by the kernel quirk
n=$(lspci -Dn | grep -c " 10de:$DEV")
if [ "$n" -eq "$N" ]; then pass "$N GPUs (10de:$DEV) enumerated"; else hard "$n/$N GPUs enumerated: cold power-cycle again before anything else"; fi
q=$(cnt 'CMP 170HX: BAR1 REBAR programmed to 64GB')
if [ "$q" -eq "$N" ]; then pass "kernel BAR1 quirk x$N"; else hard "kernel BAR1 quirk lines: $q/$N"; fi

# The driver: the cmpunlocker module, the expected build, initialized without errors
f=$(modinfo -n nvidia 2>/dev/null)
case $f in
*/updates/cmpunlocker/nvidia.ko) pass "nvidia resolves to $f" ;;
*) hard "nvidia resolves to '${f:-nothing}', not updates/cmpunlocker" ;;
esac
if [ -f "$f" ]; then
    s=$(sha256sum "$f" | cut -d' ' -f1)
    if [ -z "$SHA" ]; then
        echo "INFO: nvidia.ko sha256 $s (no expected hash given)"
    elif [ "$s" = "$SHA" ]; then
        pass "nvidia.ko sha256 $s"
    else
        hard "nvidia.ko sha256 $s != expected $SHA"
    fi
    src=$(cat /sys/module/nvidia/srcversion 2>/dev/null)
    if [ "$src" = "$(modinfo -F srcversion "$f")" ]; then pass "loaded nvidia srcversion $src"; else hard "loaded nvidia srcversion '$src' differs from $f"; fi
fi
if [ "$(cnt 'RmInitAdapter failed|rm_init_adapter failed|GPU has fallen off the bus')" -eq 0 ]; then pass "no RmInitAdapter failure"; else hard "RmInitAdapter failure or GPU fallen off the bus in the kernel log"; fi
x=$(cnt 'NVRM: Xid')
if [ "$x" -eq 0 ]; then pass "no Xid"; else hard "$x Xid lines: $(grep -E 'NVRM: Xid' <<<"$K" | head -3)"; fi

# cmpunlocker's memory-safety checks and the SM unlock
q=$(cnt 'SEC2_DEBUG_FB_LAYOUT: validated .*status=safe build=cmpunlocker-safety-v4')
if [ "$q" -eq "$N" ]; then pass "FB layout safe x$N"; else hard "FB layout safe lines $q/$N"; fi
q=$(cnt 'SEC2_DEBUG_PMA_GUARD: .*status=safe build=cmpunlocker-safety-v4')
if [ "$q" -eq "$N" ]; then pass "PMA guard safe x$N"; else hard "PMA guard safe lines $q/$N"; fi
if [ "$(cnt 'SEC2_DEBUG_FB_LAYOUT: rejected|SEC2_DEBUG_PMA_GUARD: (invalid|incomplete|unsupported|WPR metadata|final protected carveout)|FAILED to verify required')" -eq 0 ]; then
    pass "no rejected layout, PMA guard or PLM"
else
    hard "rejected layout, PMA guard or PLM line present"
fi
q=$(cnt 'SM-RECONFIG start'); p=$(cnt 'RECONFIG_PLM not opened')
if [ "$q" -eq "$N" ] && [ "$p" -eq 0 ]; then pass "SM-RECONFIG x$N"; else fail "SM-RECONFIG start $q/$N, RECONFIG_PLM not opened $p"; fi

# BAR1 P2P: the static BAR1 must start at offset 0 and cover the whole client FB on every GPU. Offset 0 shows
# that the P2P type was BAR1 when BAR1 was set up (no P2P mailbox reserved); a non-zero offset means the
# mailbox was reserved and the map is truncated. RM, nv_gpu_ops and UVM add the offset themselves.
q=$(cnt 'CMPUNLOCK_BAR1P2P: forcing static BAR1 ENABLE')
if [ "$q" -eq "$N" ]; then pass "static BAR1 forced x$N"; else hard "static BAR1 force lines $q/$N"; fi
q=$(cnt 'CMPUNLOCK_BAR1P2P: static BAR1 mapped offset 0x0 size ')
if [ "$q" -eq "$N" ]; then pass "static BAR1 mapped at offset 0 x$N"; else hard "static BAR1 at offset 0: $q/$N"; fi
if [ "$(cnt 'CMPUNLOCK_BAR1P2P: (clamping static BAR1|static BAR1 mapping failed|BAR1 offset .* beyond mappable|static BAR1 size .* does not fit)')" -eq 0 ]; then
    pass "no clamped or failed static BAR1"
else
    hard "clamped or failed static BAR1 mapping"
fi
q=$(cnt 'CMPUNLOCK_BAR1P2P: GSP P2P caps forced to OK')
if [ "$q" -ge "$N" ]; then pass "GSP P2P caps forced"; else fail "GSP caps-forced lines $q (<$N)"; fi
grep -E 'CMPUNLOCK_BAR1P2P: static BAR1 mapped' <<<"$K" | sed 's/^/  /'
topo=$(nvidia-smi topo -p2p r 2>&1)
ok=$(grep -E '^ *GPU[0-9]' <<<"$topo" | grep -o -w 'OK' | wc -l)
notok=$(grep -E '^ *GPU[0-9]' <<<"$topo" | grep -o -w -E 'NS|GNS|CNS|TNS|NA|NO|ERR|U' | wc -l)
if [ "$ok" -eq $((N * (N - 1))) ] && [ "$notok" -eq 0 ]; then pass "topo -p2p r: $ok/$((N * (N - 1))) OK"; else fail "topo -p2p r: $ok OK, $notok not OK"; fi
awk '{print "  " $0}' <<<"$topo"
if [ "$(cnt 'CMPUNLOCK_BAR1P2P: BAR1 P2P unavailable for gpuMask')" -eq 0 ]; then
    pass "no pair fell back to 'not supported'"
else
    fail "pairs without BAR1 P2P: $(grep -E 'BAR1 P2P unavailable' <<<"$K" | head -4)"
fi

# What nvidia-smi reports per GPU
q=$(nvidia-smi --query-gpu=index,memory.total,ecc.mode.current,pcie.link.gen.current,pcie.link.width.current --format=csv,noheader,nounits)
if [ "$(awk -F', ' -v m="$MEM_MIB" '$2 == m' <<<"$q" | wc -l)" -eq "$N" ]; then pass "memory.total $MEM_MIB MiB x$N"; else hard "memory.total: $(cut -d, -f2 <<<"$q" | tr '\n' ' ')"; fi
if [ "$(awk -F', ' -v g="$LINK_GEN" -v w="$LINK_WIDTH" '$4 == g && $5 == w' <<<"$q" | wc -l)" -eq "$N" ]; then
    pass "links Gen$LINK_GEN x$LINK_WIDTH x$N"
else
    fail "links: $(awk -F', ' '{print $1 ":Gen" $4 "x" $5}' <<<"$q" | tr '\n' ' ')"
fi
if [ "$ECC" = 1 ]; then e=Enabled; else e='[N/A]'; fi
if [ "$(awk -F', ' -v e="$e" '$3 == e' <<<"$q" | wc -l)" -eq "$N" ]; then pass "ecc.mode.current $e x$N"; else fail "ecc.mode.current: $(cut -d, -f3 <<<"$q" | tr '\n' ' ') (expected $e)"; fi
b=$(nvidia-smi -q -d MEMORY | awk '/BAR1 Memory Usage/ {f = 1} f && /Total/ {print $3; f = 0}')
if [ "$(grep -c -x "$BAR1_MIB" <<<"$b")" -eq "$N" ]; then pass "BAR1 Total $BAR1_MIB MiB x$N"; else hard "BAR1 Total: $(tr '\n' ' ' <<<"$b")"; fi
hmm=$(cat /sys/module/nvidia_uvm/parameters/uvm_disable_hmm 2>/dev/null || echo not-loaded)
case $hmm in
Y) pass "uvm_disable_hmm=Y" ;;
not-loaded) echo "INFO: nvidia_uvm not loaded yet; run again after the first CUDA use (or nvidia-modprobe -u -c 0)" ;;
*) fail "uvm_disable_hmm=$hmm" ;;
esac
echo "RESULT rc=$rc"
exit $rc
