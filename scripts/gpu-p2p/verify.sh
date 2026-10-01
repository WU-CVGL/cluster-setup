#!/bin/bash
# Checks after rebooting into the P2P modules; no root needed.
# usage: ./verify.sh            (V=<driver version> to expect a specific version; default: the version of
#                                /lib/modules/$(uname -r)/updates/p2p/nvidia.ko)
# Prints one PASS / FAIL / WARN / SKIP line per check and "RESULT: PASS|FAIL"; exits 1 on any FAIL.
# The kernel-log check needs membership in the adm or systemd-journal group (else SKIP).
set -uo pipefail
K=$(uname -r)
D=/lib/modules/$K/updates/p2p
MODS=(nvidia nvidia-uvm nvidia-modeset nvidia-drm nvidia-peermem)
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
warn() { echo "WARN  $*"; }
skip() { echo "SKIP  $*"; }
info() { echo "      $*"; }

# NVIDIA display-class PCI devices (sysfs names)
GPUS=()
for d in /sys/bus/pci/devices/*; do
    [ "$(cat "$d/vendor")" = 0x10de ] || continue
    case "$(cat "$d/class")" in 0x03*) GPUS+=("$(basename "$d")") ;; esac
done
echo "kernel $K, ${#GPUS[@]} NVIDIA GPUs"

# 1. installed modules and what modprobe resolves to
if [ -f "$D/nvidia.ko" ]; then
    V=${V:-$(modinfo -F version "$D/nvidia.ko")}
    pass "P2P modules installed in $D (version $V)"
    [ -f "$D/SOURCE" ] && sed -n '1,2p' "$D/SOURCE" | sed 's/^/      /'
    bad=""
    for m in "${MODS[@]}"; do
        f=$(modinfo -k "$K" -n "${m//-/_}" 2>/dev/null)
        case $f in "$D"/*) ;; *) bad+=" ${m//-/_}->${f:-none}" ;; esac
    done
    if [ -z "$bad" ]; then pass "modprobe resolves all ${#MODS[@]} modules to updates/p2p"; else fail "depmod override missing:$bad"; fi
else
    V=${V:-}
    fail "no P2P modules in $D (installed for another kernel?)"
fi

# 2. loaded driver: open kernel module, version, userspace version
if [ -r /proc/driver/nvidia/version ]; then
    line=$(head -1 /proc/driver/nvidia/version | tr -s ' ')
    info "$line"
    case $line in *"Open Kernel Module"*) pass "open kernel module loaded" ;; *) fail "loaded kernel module is not the open one" ;; esac
    if [ -n "$V" ]; then
        case $line in *" $V "*) pass "kernel module version $V" ;; *) fail "kernel module version is not $V" ;; esac
        uv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | sort -u | tr '\n' ' ')
        if [ "$uv" = "$V " ]; then pass "userspace driver (nvidia-smi) $V"; else fail "userspace driver reports '${uv% }', expected $V"; fi
    fi
else
    fail "nvidia module not loaded (/proc/driver/nvidia/version missing)"
fi

# 3. loaded modules are the P2P build (srcversion)
for m in "${MODS[@]}"; do
    mod=${m//-/_}
    [ -f "$D/$m.ko" ] || continue
    if [ ! -r "/sys/module/$mod/srcversion" ]; then
        [ "$mod" = nvidia_peermem ] && { info "$mod not loaded (only needed for GPUDirect RDMA)"; continue; }
        fail "$mod not loaded"
        continue
    fi
    loaded=$(cat "/sys/module/$mod/srcversion")
    p2p=$(modinfo -F srcversion "$D/$m.ko")
    dkms=$(modinfo -F srcversion "/lib/modules/$K/updates/dkms/$m.ko" 2>/dev/null)
    if [ "$loaded" = "$p2p" ] && [ "$p2p" = "$dkms" ]; then
        warn "$mod: srcversion is the same for the P2P and the DKMS build, cannot tell them apart (see BAR1 / topo checks)"
    elif [ "$loaded" = "$p2p" ]; then
        pass "$mod loaded from the P2P build (srcversion $loaded)"
    elif [ -n "$dkms" ] && [ "$loaded" = "$dkms" ]; then
        fail "$mod is the stock DKMS build (srcversion $loaded)"
    else
        fail "$mod srcversion $loaded matches neither the P2P ($p2p) nor the DKMS build"
    fi
done
info "(nvidia.ko's srcversion covers only the interface layer, not the RM core that carries most of the patch)"

# 4. BAR1 resized (the patch resizes BAR1 to the largest size the GPU advertises)
if bar=$(nvidia-smi -q -d MEMORY 2>/dev/null | awk '
        /^GPU / {gpu = $2}
        /FB Memory Usage/ {sec = "fb"} /BAR1 Memory Usage/ {sec = "bar1"}
        /^ *Total *:/ && sec != "" {v[sec] = $3; if (sec == "bar1") print gpu, v["fb"], v["bar1"]; sec = ""}') && [ -n "$bar" ]; then
    small=$(echo "$bar" | awk '$3 <= 256 {printf " %s(BAR1 %s MiB)", $1, $3}')
    if [ -n "$small" ]; then
        fail "BAR1 not resized:$small"
    else
        echo "$bar" | awk '{k = $3 >= $2 ? "BAR1 >= VRAM: static BAR1 P2P" : "BAR1 < VRAM: needs dynamic BAR1 P2P"; c[$3 " MiB, VRAM " $2 " MiB (" k ")"]++}
            END {for (x in c) printf "PASS  BAR1 %s on %d GPUs\n", x, c[x]}'
    fi
else
    fail "nvidia-smi -q -d MEMORY failed"
fi

# 5. IOMMU passthrough
if grep -qw 'iommu=pt' /proc/cmdline; then
    pass "iommu=pt on the kernel command line"
else
    fail "no iommu=pt on the kernel command line: $(grep -oE '[a-z_]*iommu=[^ ]*' /proc/cmdline | tr '\n' ' ')"
fi
types=""
for g in "${GPUS[@]}"; do
    t=$(cat "/sys/bus/pci/devices/$g/iommu_group/type" 2>/dev/null || echo "no-iommu-group")
    types+="$t"$'\n'
    case $t in identity | no-iommu-group) ;; *) fail "$g IOMMU domain type '$t' (expected identity)" ;; esac
done
summary=$(printf '%s' "$types" | sort | uniq -c | awk '{printf "%s x%s ", $2, $1}')
printf '%s' "$types" | grep -qvE '^(identity|no-iommu-group)$' || pass "GPU IOMMU domains: $summary"

# 6. UVM HMM off (see install-p2p-modules.sh / the HMM + iommu=pt DMA32 issue)
if [ -r /sys/module/nvidia_uvm/parameters/uvm_disable_hmm ]; then
    h=$(cat /sys/module/nvidia_uvm/parameters/uvm_disable_hmm)
    if [ "$h" = Y ]; then pass "nvidia_uvm uvm_disable_hmm=Y"; else fail "nvidia_uvm uvm_disable_hmm=$h (HMM on; host cuMem allocations fail under iommu=pt)"; fi
else
    skip "nvidia_uvm parameter uvm_disable_hmm not readable"
fi
n=$(grep -c 'nvidia-uvm-hmm' /proc/iomem 2>/dev/null)
if [ "${n:-0}" = 0 ]; then pass "no nvidia-uvm-hmm regions in /proc/iomem"; else fail "$n nvidia-uvm-hmm regions in /proc/iomem (a reboot is needed after disabling HMM)"; fi

# 7. P2P capability reported by the driver
if topo=$(nvidia-smi topo -p2p r 2>/dev/null); then
    notok=$(echo "$topo" | awk '$1 ~ /^GPU[0-9]+$/ && $2 !~ /^GPU/ {for (i = 2; i <= NF; i++) if ($i != "X" && $i != "OK") {b = b " " $1 "->GPU" (i - 2) "=" $i}} END {print b}')
    if [ -z "$notok" ]; then pass "nvidia-smi topo -p2p r: OK for all GPU pairs"; else fail "nvidia-smi topo -p2p r:$notok"; fi
else
    fail "nvidia-smi topo -p2p r failed"
fi

# 8. PCIe link width (at idle; run_host.sh samples under load)
narrow=$(nvidia-smi --query-gpu=index,pcie.link.width.current,pcie.link.width.max --format=csv,noheader,nounits 2>/dev/null |
    awk -F', *' '$2 < $3 {printf " GPU%s x%s/x%s", $1, $2, $3}')
if [ -n "$narrow" ]; then warn "PCIe link narrower than the maximum:$narrow (caps every transfer and NCCL ring through it)"; else pass "PCIe links at full width"; fi

# 9. kernel log of this boot
if klog=$(journalctl -k -b --no-pager -q 2>/dev/null) && [ -n "$klog" ]; then
    src=journalctl
elif klog=$(dmesg 2>/dev/null) && [ -n "$klog" ]; then
    src=dmesg
else
    klog=""
fi
if [ -z "$klog" ]; then
    skip "kernel log not readable (join the adm or systemd-journal group, or run with sudo)"
else
    xid=$(echo "$klog" | grep -E 'NVRM: Xid|Assert failed' || true)
    m3=$(echo "$klog" | grep -E 'METHOD3' || true)
    if [ -n "$xid" ]; then
        fail "kernel log ($src): $(echo "$xid" | wc -l) Xid / assert lines"
        echo "$xid" | tail -5 | cut -c1-200 | sed 's/^/      /'
    else
        pass "kernel log ($src): no Xid or assert"
    fi
    if [ -n "$m3" ]; then
        warn "kernel log: $(echo "$m3" | wc -l) METHOD3 lines (dynamic BAR1 window errors)"
        echo "$m3" | tail -5 | cut -c1-200 | sed 's/^/      /'
    fi
fi

# 10. package hold (the modules are pinned to one driver version)
if [ -n "$V" ]; then
    pkg=nvidia-driver-${V%%.*}-open
    if apt-mark showhold 2>/dev/null | grep -qx "$pkg"; then pass "$pkg is held"; else warn "$pkg is not held: an upgrade would break the driver/library version match"; fi
fi

echo
if [ "$fails" = 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL ($fails checks)"
    exit 1
fi
