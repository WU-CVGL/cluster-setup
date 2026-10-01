#!/bin/bash
# Install P2P-patched NVIDIA open kernel modules next to the Ubuntu DKMS build, or undo it.
#
# usage: sudo SRC=<built kernel-open dir> [K=<kernel>] [V=<driver version>] ./install-p2p-modules.sh [install]
#        sudo [K=<kernel>] ./install-p2p-modules.sh --restore
#   options instead of the environment variables: --src DIR  --kernel K  --version V
#   K   kernel the modules are for (default: the running kernel, uname -r)
#   V   driver version (default: modinfo version of $SRC/nvidia.ko); the Ubuntu package
#       nvidia-driver-<N>-open must be installed at this exact version
#   SRC the kernel-open directory of the patched source tree after `make modules` (absolute path)
#
# install:
#   - refuses when apt/dpkg is running or holds its lock, Secure Boot is on, the installed
#     nvidia-driver-*-open package is not version V, a .ko is not version V or not built for K, or GRUB
#     already sets another iommu= mode
#   - backs up /etc/default/grub (once) and the package list to /var/backups/nvidia-p2p/
#   - disables UVM HMM (/etc/modprobe.d/nvidia-uvm-hmm.conf)
#   - adds "amd_iommu=on iommu=pt" (AMD) or "intel_iommu=on iommu=pt" (Intel) to GRUB_CMDLINE_LINUX_DEFAULT
#     (records the parameters it added), runs update-grub and checks that the default boot entry is K
#     with iommu=pt
#   - apt-mark holds the installed packages of the driver branch (the modules are pinned to version V)
#   - only then installs nvidia, nvidia-uvm, nvidia-modeset, nvidia-drm, nvidia-peermem .ko into
#     /lib/modules/K/updates/p2p (+ SOURCE file), overrides the DKMS build via /etc/depmod.d/nvidia-p2p.conf,
#     runs depmod, checks that every module resolves to updates/p2p and regenerates the initramfs of K.
#     If anything fails after the override is written, the override for K is removed again, so a
#     failed run never leaves the P2P modules armed without iommu=pt on the default boot entry.
# --restore: removes the modules and the override for K. Unless other kernels still have P2P modules, it
#   also removes the GRUB parameters this script added, unholds the packages it held, and removes the
#   HMM setting, but only when iommu=pt is no longer on the GRUB command line (HMM + iommu=pt breaks
#   host cuMem allocations with the stock open driver too). The stock DKMS modules load after a reboot.
# The script never reboots. Reboot afterwards and run verify.sh.
set -euo pipefail

MODS=(nvidia nvidia-uvm nvidia-modeset nvidia-drm nvidia-peermem)   # file names (with dashes)
STATE=/var/backups/nvidia-p2p
DEPMOD_CONF=/etc/depmod.d/nvidia-p2p.conf
HMM_CONF=/etc/modprobe.d/nvidia-uvm-hmm.conf
GRUB=/etc/default/grub
GRUB_CFG=/boot/grub/grub.cfg
GRUB_BAK=$STATE/grub.pre-p2p
GRUB_ADDED=$STATE/grub-added-params.txt
HELD=$STATE/held-packages.txt

usage() { sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "== $*"; }

mode=install
K=${K:-}
V=${V:-}
SRC=${SRC:-}
while [ $# -gt 0 ]; do
    case $1 in
    install) mode=install ;;
    --restore | restore) mode=restore ;;
    --src) SRC=${2:?--src needs a directory}; shift ;;
    --kernel) K=${2:?--kernel needs a kernel version}; shift ;;
    --version) V=${2:?--version needs a driver version}; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
    esac
    shift
done
K=${K:-$(uname -r)}
D=/lib/modules/$K/updates/p2p
TS=$(date +%Y%m%d-%H%M%S)

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ -d "/lib/modules/$K" ] || die "/lib/modules/$K does not exist"
# unattended-upgrade by its command line: its 15-character process name also matches the idle
# unattended-upgrade-shutdown helper, which runs all the time and holds no lock.
if pgrep -x 'apt|apt-get|aptitude|dpkg' >/dev/null || pgrep -f '/unattended-upgrade( |$)' >/dev/null; then
    die "apt/dpkg is running; wait until it has finished"
fi
if command -v fuser >/dev/null &&
    fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then
    die "the dpkg/apt lock is held (packagekitd, apt.systemd.daily, ...); wait until apt has finished"
fi
[ -z "$(dpkg --audit 2>/dev/null)" ] || die "dpkg reports unfinished packages: run 'dpkg --configure -a' first"

# Override lines of the depmod configuration that belong to other kernels.
other_kernel_overrides() {
    [ -f "$DEPMOD_CONF" ] || return 0
    awk -v k="$K" '$1 == "override" && $3 != k' "$DEPMOD_CONF"
}

write_depmod_conf() {   # $1: override lines to keep/add
    if [ -z "$1" ]; then
        rm -f "$DEPMOD_CONF"
        return
    fi
    cat >"$DEPMOD_CONF" <<'EOF'
# P2P-patched NVIDIA open kernel modules in /lib/modules/<kernel>/updates/p2p, preferred over the
# Ubuntu DKMS build in updates/dkms of the same kernel (both live under "updates", so depmod needs an
# explicit override). Written by scripts/gpu-p2p/install-p2p-modules.sh; undo with its --restore.
# depmod matches the module FILE name with dashes (nvidia-uvm), not the module name (nvidia_uvm):
# with underscores only "nvidia" (where both are equal) would be overridden.
EOF
    printf '%s\n' "$1" >>"$DEPMOD_CONF"
}

# Rewrites GRUB_CMDLINE_LINUX_DEFAULT (a simple double-quoted value) to $1.
set_grub_default_cmdline() {
    local tmp
    tmp=$(mktemp)
    NEW=$1 awk '/^GRUB_CMDLINE_LINUX_DEFAULT=/ {print "GRUB_CMDLINE_LINUX_DEFAULT=\"" ENVIRON["NEW"] "\""; next} {print}' "$GRUB" >"$tmp"
    cat "$tmp" >"$GRUB"
    rm -f "$tmp"
}

# Set while the depmod override of $K is written but the install has not finished: on any exit with an
# error the override for $K is removed again (the stock DKMS modules load on the next boot).
armed=0
on_exit() {
    local rc=$?
    if [ "$rc" != 0 ] && [ "$armed" = 1 ]; then
        armed=0
        write_depmod_conf "$(other_kernel_overrides)" || true
        depmod -a "$K" || true
        echo "ERROR: install failed after the depmod override was written; the override for $K was removed," >&2
        echo "       the stock DKMS modules load on the next boot (the files in $D stay)" >&2
    fi
    exit "$rc"
}
trap on_exit EXIT

install_modules() {
    [ -n "$SRC" ] || die "SRC (the built kernel-open directory) is required"
    [ -f "$SRC/nvidia.ko" ] || die "no $SRC/nvidia.ko: build the modules first (make modules)"
    [ -n "$V" ] || V=$(modinfo -F version "$SRC/nvidia.ko")
    local major=${V%%.*} m f pkgver params vendor

    say "preflight (kernel $K, driver $V, source $SRC)"
    if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -q 'SecureBoot enabled'; then
        die "Secure Boot is enabled: unsigned modules would not load (disable it or sign the modules)"
    fi
    pkgver=$(dpkg-query -W -f='${db:Status-Abbrev}${Version}' "nvidia-driver-$major-open" 2>/dev/null || true)
    case $pkgver in
    "ii $V"-* | "hi $V"-*) echo "  nvidia-driver-$major-open ${pkgver#* }" ;;
    *) die "nvidia-driver-$major-open $V-* is not installed (found: '${pkgver:-none}'); the userspace driver must be the same version as the modules" ;;
    esac
    for m in "${MODS[@]}"; do
        f=$SRC/$m.ko
        [ -f "$f" ] || die "missing $f"
        [ "$(modinfo -F version "$f")" = "$V" ] || die "$f is version $(modinfo -F version "$f"), not $V"
        [ "$(modinfo -F vermagic "$f" | cut -d' ' -f1)" = "$K" ] || die "$f is built for $(modinfo -F vermagic "$f" | cut -d' ' -f1), not $K"
    done
    if [ ! -f "$GRUB" ] || ! command -v update-grub >/dev/null; then
        die "no $GRUB / update-grub: only GRUB systems are supported"
    fi
    if grep -E '^GRUB_CMDLINE_LINUX(_DEFAULT)?=' "$GRUB" | grep -qE '(^|[ "=])(iommu=[^p ]|iommu=p[^t]|amd_iommu=off|intel_iommu=off)'; then
        die "$GRUB already sets a different IOMMU mode: $(grep -E '^GRUB_CMDLINE_LINUX' "$GRUB" | tr '\n' ' ')"
    fi
    [ "$(grep -c '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB")" -le 1 ] || die "several GRUB_CMDLINE_LINUX_DEFAULT lines in $GRUB: edit by hand"
    if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" && ! grep -qE '^GRUB_CMDLINE_LINUX_DEFAULT="[^"]*"$' "$GRUB"; then
        die "GRUB_CMDLINE_LINUX_DEFAULT in $GRUB is not a simple double-quoted value: edit by hand"
    fi
    vendor=$(awk -F': *' '/^vendor_id/ {print $2; exit}' /proc/cpuinfo)
    case $vendor in
    AuthenticAMD) params="amd_iommu=on iommu=pt" ;;
    GenuineIntel) params="intel_iommu=on iommu=pt" ;;
    *) die "unknown CPU vendor '$vendor'" ;;
    esac
    if ! f=$(modinfo -k "$K" -n nvidia 2>/dev/null) || [ -z "$f" ]; then
        echo "  WARNING: no stock nvidia module for $K (DKMS): --restore or a failed load would leave no driver"
    fi

    say "backups in $STATE"
    install -d -m 700 "$STATE"
    if [ -e "$GRUB_BAK" ]; then
        echo "  keeping the existing $GRUB_BAK (taken before the first install)"
    else
        cp -p "$GRUB" "$GRUB_BAK"
        echo "  $GRUB -> $GRUB_BAK"
    fi
    dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package} ${Version}\n' '*nvidia*' >"$STATE/packages-$TS.txt" 2>/dev/null || true
    echo "  package list -> $STATE/packages-$TS.txt"
    if [ -d "$D" ]; then
        install -d -m 700 "$STATE/$K/p2p-$TS"
        cp -pr "$D"/. "$STATE/$K/p2p-$TS/"
        echo "  previous $D -> $STATE/$K/p2p-$TS"
    fi

    # Harmless steps first; the modules are armed (depmod override) only after all of them succeeded.
    say "UVM HMM off ($HMM_CONF)"
    cat >"$HMM_CONF" <<'EOF'
# Written by scripts/gpu-p2p/install-p2p-modules.sh (undo: --restore). Needs a reboot to take effect.
# With the open driver, UVM HMM registers device-private memory for every GPU at the top of the
# physical address space. That inflates the NUMA node spans, so nv_get_max_sysmem_address() exceeds the
# GPU DMA mask, and with direct DMA (iommu=pt) the driver then adds GFP_DMA32 to host allocations: every
# cuMemCreate host allocation comes from the ~2 GiB ZONE_DMA32 on node 0 and fails. This is a bug of the
# stock open driver, not of the P2P patches: keep this file as long as iommu=pt is on the command line.
# Cost of this option: no HMM (GPU access to plain malloc'd pageable memory).
options nvidia-uvm uvm_disable_hmm=1
EOF

    say "kernel command line: $params"
    local cur new p added=""
    cur=$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/\1/p' "$GRUB")
    new=$cur
    for p in $params; do
        case " $new " in *" $p "*) ;; *) new=${new:+$new }$p; added+=" $p" ;; esac
    done
    if [ -z "$added" ] && grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB"; then
        echo "  already set"
    elif grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB"; then
        set_grub_default_cmdline "$new"
    else
        echo "GRUB_CMDLINE_LINUX_DEFAULT=\"$new\"" >>"$GRUB"
    fi
    if [ -n "$added" ]; then
        # shellcheck disable=SC2086
        { printf '%s\n' $added; cat "$GRUB_ADDED" 2>/dev/null || true; } | sort -u >"$GRUB_ADDED.new"
        mv "$GRUB_ADDED.new" "$GRUB_ADDED"
        echo "  added:$added (recorded in $GRUB_ADDED for --restore)"
    fi
    grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" | sed 's/^/  /'
    update-grub

    say "default boot entry"
    local gd first
    gd=$(sed -n 's/^GRUB_DEFAULT=//p' "$GRUB" | tail -n1 | tr -d "\"'")
    first=$(grep -m1 -E '^\s*linux\s' "$GRUB_CFG" || true)
    if [ -z "$gd" ] || [ "$gd" = 0 ]; then
        echo "  $(echo "$first" | awk '{print $2}') ...$(echo "$first" | grep -oE '(amd_|intel_)?iommu=[^ ]*' | tr '\n' ' ' | sed 's/^/ /')"
        echo "$first" | grep -qE "vmlinuz-$K .*iommu=pt" ||
            die "the default boot entry (first entry, GRUB_DEFAULT=${gd:-0}) is not $K with iommu=pt: fix GRUB, then run this again (a newer kernel installed? an /etc/default/grub.d/*.cfg overriding GRUB_CMDLINE_LINUX_DEFAULT?). No module was installed."
    else
        grep -E '^\s*linux\s' "$GRUB_CFG" | grep -qE "vmlinuz-$K .*iommu=pt" ||
            die "no boot entry for $K with iommu=pt in $GRUB_CFG. No module was installed."
        echo "  WARNING: GRUB_DEFAULT=$gd: check by hand that the default entry boots $K before rebooting"
    fi

    say "hold the driver packages of branch $major (the modules are built for $V only)"
    local pkgs held newly arch
    arch=$(dpkg --print-architecture)
    # dpkg-query prints Multi-Arch: same packages arch-qualified, apt-mark showhold prints native ones
    # without the architecture: strip the native one so both lists compare.
    pkgs=$(dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' 2>/dev/null |
        awk -v m="$major" '($1 == "ii" || $1 == "hi") && $2 ~ /nvidia/ && $2 ~ ("-" m "([-:]|$)") {print $2}' |
        sed "s/:$arch\$//")
    held=$(apt-mark showhold)
    newly=$(comm -23 <(printf '%s\n' "$pkgs" | sort -u) <(printf '%s\n' "$held" | sort -u) | grep . || true)
    if [ -n "$newly" ]; then
        # shellcheck disable=SC2086
        apt-mark hold $newly
        { printf '%s\n' "$newly"; cat "$HELD" 2>/dev/null || true; } | sort -u >"$HELD.new"
        mv "$HELD.new" "$HELD"
    else
        echo "  already held: $(printf '%s\n' "$pkgs" | tr '\n' ' ')"
    fi

    say "modules -> $D"
    install -d -m 755 "$D"
    for m in "${MODS[@]}"; do install -m 644 "$SRC/$m.ko" "$D/$m.ko"; done
    local commit=""
    if [ -f "$SRC/../.source-commit" ]; then
        commit=$(tr -s '\n' ' ' <"$SRC/../.source-commit")
    elif commit=$(git -c safe.directory='*' -C "$SRC/.." rev-parse HEAD 2>/dev/null); then
        :
    else
        commit=""
    fi
    {
        echo "P2P-patched NVIDIA open kernel modules $V for $K"
        echo "source: $(readlink -f "$SRC")${commit:+ @ $commit}"
        echo "installed: $(date -Is) by install-p2p-modules.sh"
        (cd "$D" && sha256sum "${MODS[@]/%/.ko}")
    } >"$D/SOURCE"
    sed 's/^/  /' "$D/SOURCE"

    say "depmod override $DEPMOD_CONF"
    local lines
    lines=$(other_kernel_overrides)
    for m in "${MODS[@]}"; do lines+=${lines:+$'\n'}"override $m $K updates/p2p"; done
    armed=1
    write_depmod_conf "$lines"
    depmod -a "$K"
    for m in "${MODS[@]}"; do
        f=$(modinfo -k "$K" -n "${m//-/_}")
        case $f in
        "$D"/*) echo "  ${m//-/_} -> $f" ;;
        *) die "the override did not apply to ${m//-/_} ($f)" ;;
        esac
    done

    say "initramfs for $K"
    update-initramfs -u -k "$K"
    armed=0

    echo
    echo "DONE. Nothing is active yet: reboot (sudo systemctl reboot), then run verify.sh."
}

# Is iommu=pt still on a GRUB command line of $GRUB or /etc/default/grub.d/*.cfg?
grub_has_iommu_pt() {
    { cat "$GRUB"; cat /etc/default/grub.d/*.cfg 2>/dev/null || true; } |
        grep -E '^GRUB_CMDLINE_LINUX(_DEFAULT)?=' | grep -qE '(^|[ "=])iommu=pt([ "]|$)'
}

restore_modules() {
    local others m f
    others=$(other_kernel_overrides)
    say "remove the P2P modules of $K"
    if [ -d "$D" ]; then
        install -d -m 700 "$STATE/$K/p2p-$TS"
        cp -pr "$D"/. "$STATE/$K/p2p-$TS/"
        rm -r "$D"
        echo "  $D removed (copy in $STATE/$K/p2p-$TS)"
    else
        echo "  $D does not exist"
    fi
    write_depmod_conf "$others"
    depmod -a "$K"
    for m in "${MODS[@]}"; do
        f=$(modinfo -k "$K" -n "${m//-/_}" 2>/dev/null || true)
        echo "  ${m//-/_} -> ${f:-NOT FOUND}"
    done
    [ -n "$(modinfo -k "$K" -n nvidia 2>/dev/null || true)" ] ||
        echo "  WARNING: no nvidia module left for $K: reinstall the driver package (DKMS) before rebooting"

    if [ -n "$others" ]; then
        say "other kernels still use P2P modules: keeping HMM off, the GRUB settings and the package holds"
        printf '%s\n' "$others" | awk '{print "  " $3}' | sort -u
    else
        say "GRUB: remove the parameters added by the install"
        if [ -s "$GRUB_ADDED" ]; then
            local cur new p
            cur=$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/\1/p' "$GRUB")
            new=" $cur "
            while read -r p; do
                [ -n "$p" ] || continue
                new=${new// "$p" / }
            done <"$GRUB_ADDED"
            new=$(echo "$new" | tr -s ' ' | sed 's/^ //; s/ $//')
            if [ "$new" != "$cur" ]; then
                cp -p "$GRUB" "$STATE/grub.before-restore-$TS"
                set_grub_default_cmdline "$new"
                echo "  removed: $(tr '\n' ' ' <"$GRUB_ADDED")(previous file: $STATE/grub.before-restore-$TS)"
                grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" | sed 's/^/  /'
                update-grub
            else
                echo "  none of the recorded parameters is set any more"
            fi
            mv "$GRUB_ADDED" "$GRUB_ADDED.restored-$TS"
        else
            echo "  no parameters recorded in $GRUB_ADDED (set by hand or by an older version of this script):"
            echo "  GRUB unchanged; remove iommu parameters by hand if wanted (file before the first install: $GRUB_BAK)"
        fi
        if grub_has_iommu_pt; then
            say "UVM HMM stays off: iommu=pt is still on the GRUB command line"
            echo "  HMM + iommu=pt breaks host cuMem allocations with the stock open driver too (NCCL SHM crashes)."
            echo "  Remove $HMM_CONF only together with iommu=pt."
        else
            say "UVM HMM back on"
            rm -f "$HMM_CONF"
        fi
        say "package holds"
        if [ -s "$HELD" ]; then
            # shellcheck disable=SC2046
            apt-mark unhold $(cat "$HELD")
            mv "$HELD" "$HELD.restored-$TS"
        else
            echo "  none recorded (holds set by hand stay: apt-mark showhold)"
        fi
    fi
    say "initramfs for $K"
    update-initramfs -u -k "$K"

    echo
    echo "DONE. Reboot (sudo systemctl reboot) to load the stock modules."
    echo "To go back to an older driver as well: apt-get install it (package lists in $STATE/packages-*.txt)."
}

case $mode in
install) install_modules ;;
restore) restore_modules ;;
esac
