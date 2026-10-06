#!/bin/bash
# Saved copies of the cmpunlocker module tree, to switch between builds and to roll back.
# usage:
#   sudo ./trees.sh save <label>       copy the live tree (/lib/modules/<K>/updates/cmpunlocker), the initramfs
#                                      and the boot configuration (/etc/modprobe.d, /etc/default/grub and the
#                                      units below) to $STORE/<label>, then check the copy
#   sudo ./trees.sh activate <label>   make $STORE/<label> the live tree: rsync, depmod, update-initramfs;
#                                      it takes effect at the next cold power cycle
#   sudo ./trees.sh show               sha256 and srcversion of the live nvidia.ko, what modprobe resolves,
#                                      what is loaded, and the saved trees (without root: no saved trees)
# K: kernel release (default: uname -r; set it when running from another kernel, e.g. the GRUB fallback).
# STORE: where the trees are kept (default /root/cmpunlocker-trees).
# activate restores the module tree only; the saved boot configuration in $STORE/<label>/etc is for
# comparing and restoring by hand. srcversion does not tell cmpunlocker builds apart; the sha256 of
# nvidia.ko does.
set -euo pipefail
K=${K:-$(uname -r)}
LIVE=/lib/modules/$K/updates/cmpunlocker
STORE=${STORE:-/root/cmpunlocker-trees}
UNITS=(gen2.service cmpunlocker-gen2.service cmpunlocker-passthrough.service gpu-acs-redir-off.service)

sha() { sha256sum "$1" | cut -d' ' -f1; }
need_root() { [ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }; }

case "${1:-}" in
save)
    need_root
    d=$STORE/${2:?label}
    [ ! -e "$d" ] || { echo "refusing: $d exists"; exit 1; }
    [ -f "$LIVE/nvidia.ko" ] || { echo "no live tree in $LIVE"; exit 1; }
    mkdir -p "$d/etc/systemd"
    cp -a "$LIVE" "$d/cmpunlocker"
    cp -a "/boot/initrd.img-$K" "$d/"
    cp -a /etc/modprobe.d /etc/default/grub "$d/etc/"
    for u in "${UNITS[@]}"; do
        if [ -e "/etc/systemd/system/$u" ]; then cp -a "/etc/systemd/system/$u" "$d/etc/systemd/"; fi
    done
    (cd "$LIVE" && find . -type f -exec sha256sum {} +) >"$d/cmpunlocker.sha256"
    (cd "$d/cmpunlocker" && sha256sum -c --quiet ../cmpunlocker.sha256)
    echo "saved $d: nvidia.ko sha256 $(sha "$d/cmpunlocker/nvidia.ko")"
    ;;
activate)
    need_root
    d=$STORE/${2:?label}
    [ -f "$d/cmpunlocker/nvidia.ko" ] || { echo "no tree in $d"; exit 1; }
    mkdir -p "$LIVE"
    rsync -a --checksum --delete "$d/cmpunlocker/" "$LIVE/"
    depmod -a "$K"
    update-initramfs -u -k "$K"
    want=$(sha "$d/cmpunlocker/nvidia.ko")
    f=$(modinfo -k "$K" -n nvidia 2>/dev/null || true)
    got=$(if [ -f "$f" ]; then sha "$f"; else echo none; fi)
    if [ "$f" != "$LIVE/nvidia.ko" ] || [ "$got" != "$want" ]; then
        echo "ACTIVATE FAILED: modprobe resolves '$f' (sha256 $got), want $LIVE/nvidia.ko with $want"
        exit 1
    fi
    echo "active: $2 (nvidia.ko sha256 $want); takes effect at the next cold power cycle"
    ;;
show)
    echo "kernel $K"
    if [ -f "$LIVE/nvidia.ko" ]; then
        echo "live tree $LIVE: nvidia.ko sha256 $(sha "$LIVE/nvidia.ko"), srcversion $(modinfo -F srcversion "$LIVE/nvidia.ko")"
    else
        echo "live tree $LIVE: none"
    fi
    echo "modprobe resolves nvidia to $(modinfo -k "$K" -n nvidia 2>/dev/null || echo nothing)"
    echo "loaded nvidia srcversion: $(cat /sys/module/nvidia/srcversion 2>/dev/null || echo none)"
    if [ -d "$STORE" ]; then
        for t in "$STORE"/*/cmpunlocker/nvidia.ko; do
            if [ -f "$t" ]; then echo "saved $(basename "$(dirname "$(dirname "$t")")"): nvidia.ko sha256 $(sha "$t")"; fi
        done
    fi
    ;;
*)
    sed -n '2,15p' "$0"
    exit 1
    ;;
esac
