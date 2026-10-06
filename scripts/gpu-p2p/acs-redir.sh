#!/bin/bash
# PCIe ACS P2P redirect on the bridges above the NVIDIA GPUs, at runtime (no BIOS change, no reboot).
# usage: sudo ./acs-redir.sh status|off|restore
#   status   ACS Control of every bridge between a GPU and its root complex, and the persistent
#            kernel-parameter equivalent of "off"
#   off      clears Request Redirect, Completion Redirect and Egress Control (the bits the kernel's
#            pci=disable_acs_redir= clears); other ACS bits stay. Saves the old values first.
#            Refuses unless every GPU is in an identity IOMMU domain (iommu=pt) or has no IOMMU group:
#            with a translating IOMMU, peer requests carry IOVAs, and a PCIe switch could route them
#            directly to the wrong device without translation. ACS_FORCE=1 overrides.
#   restore  writes back the saved values. A reboot also restores the kernel's defaults.
# Linux enables the redirect bits whenever an IOMMU driver is active (also with iommu=pt), so a BIOS
# ACS switch may not stick. ACS_STATE overrides the file with the saved values.
set -euo pipefail
STATE=${ACS_STATE:-/root/acs-redir-saved.txt}
MASK=$(((1 << 2) | (1 << 3) | (1 << 5)))   # RR | CR | EC in the ACS Control register

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
case "${1:-}" in status | off | restore) ;; *) echo "usage: $0 status|off|restore"; exit 1 ;; esac
command -v setpci >/dev/null || { echo "setpci not found (apt install pciutils)"; exit 1; }

# Every bridge between an NVIDIA display-class device (VGA 0x0300, 3D 0x0302) and its root complex.
gpu_bridges() {
    local d p
    for d in /sys/bus/pci/devices/*; do
        [ "$(cat "$d/vendor")" = 0x10de ] || continue
        case "$(cat "$d/class")" in 0x03*) ;; *) continue ;; esac
        p=$(readlink -f "$d/..")
        while [ -e "$p/config" ]; do
            basename "$p"
            p=$(readlink -f "$p/..")
        done
    done | sort -u
}

pci_id() { printf '%s:%s' "$(sed 's/^0x//' "/sys/bus/pci/devices/$1/vendor")" "$(sed 's/^0x//' "/sys/bus/pci/devices/$1/device")"; }

if [ "$1" = off ]; then
    for d in /sys/bus/pci/devices/*; do
        [ "$(cat "$d/vendor")" = 0x10de ] || continue
        case "$(cat "$d/class")" in 0x03*) ;; *) continue ;; esac
        t=$(cat "$d/iommu_group/type" 2>/dev/null || echo none)
        case $t in
        identity | none) ;;
        *)
            if [ "${ACS_FORCE:-0}" != 1 ]; then
                echo "GPU $(basename "$d") is in IOMMU domain '$t': clearing ACS redirect is only safe with iommu=pt"
                echo "(identity domains; see verify.sh). ACS_FORCE=1 overrides."
                exit 1
            fi
            ;;
        esac
    done
fi

bridges=$(gpu_bridges)
[ -n "$bridges" ] || { echo "no bridges above NVIDIA GPUs found"; exit 1; }
acs_bridges=()
for b in $bridges; do
    if ! cur=$(setpci -s "$b" ECAP_ACS+6.w 2>/dev/null) || [ -z "$cur" ]; then
        echo "$b [$(pci_id "$b")]: no ACS capability"
        continue
    fi
    acs_bridges+=("$b")
    v=$((0x$cur))
    case "$1" in
    status)
        printf '%s [%s] ACSCtl=0x%04x  RR=%d CR=%d EC=%d\n' "$b" "$(pci_id "$b")" "$v" $((v >> 2 & 1)) $((v >> 3 & 1)) $((v >> 5 & 1)) ;;
    off)
        grep -q "^$b " "$STATE" 2>/dev/null || echo "$b $cur" >>"$STATE"
        setpci -s "$b" ECAP_ACS+6.w="$(printf %04x $((v & ~MASK)))"
        echo "$b 0x$cur -> 0x$(setpci -s "$b" ECAP_ACS+6.w)" ;;
    restore)
        old=$(awk -v b="$b" '$1 == b {print $2}' "$STATE" 2>/dev/null || true)
        [ -n "$old" ] || { echo "$b: nothing saved"; continue; }
        setpci -s "$b" ECAP_ACS+6.w="$old"
        echo "$b -> 0x$(setpci -s "$b" ECAP_ACS+6.w)" ;;
    esac
done
if [ "$1" = restore ] && [ -f "$STATE" ]; then mv "$STATE" "$STATE.restored"; fi

if [ "$1" = status ] && [ ${#acs_bridges[@]} -gt 0 ]; then
    # The kernel parameter pci=disable_acs_redir=pci:<vendor>:<device> applies to every device with that ID.
    # The bridges with ACS above the GPUs are usually just the root ports; with PCIe switches the switch
    # ports are included here too, since their redirect bits matter the same way.
    ids=$(for b in "${acs_bridges[@]}"; do pci_id "$b"; echo; done | sort -u)
    echo
    if [ "$(printf '%s\n' "$ids" | wc -l)" = 1 ]; then
        others=0
        for d in /sys/bus/pci/devices/*; do
            n=$(basename "$d")
            [ "$(pci_id "$n")" = "$ids" ] || continue
            printf '%s\n' "${acs_bridges[@]}" | grep -qx "$n" || others=$((others + 1))
        done
        echo "Persistent equivalent of 'off' (all ${#acs_bridges[@]} bridges with ACS above the GPUs are $ids):"
        echo "  add to GRUB_CMDLINE_LINUX_DEFAULT: pci=disable_acs_redir=pci:$ids"
        echo "  only with iommu=pt (identity IOMMU domains for the GPUs, check with verify.sh)"
        [ "$others" = 0 ] || echo "  note: $others other device(s) not above a GPU have the same ID and would be affected too"
    else
        echo "The bridges with ACS above the GPUs have different IDs: $(printf '%s\n' "$ids" | tr '\n' ' ')"
        echo "  a persistent setting needs a ';'-separated pci=disable_acs_redir= list, which GRUB would split"
        echo "  at the ';' unless it is quoted; run 'off' at every boot instead (gpu-acs-redir-off.service),"
        echo "  or quote it carefully."
    fi
fi
exit 0
