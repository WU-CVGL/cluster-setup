#!/bin/bash
# gpu-unlock Jammy integration: a thin udev wrapper follows the current vendor at each build.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
install -d -m 755 /etc/initramfs-tools/hooks /etc/initramfs-tools/scripts/init-top
wrapper=/etc/initramfs-tools/scripts/init-top/udev
if [ -e "$wrapper" ] && ! grep -q 'gpu-unlock site wrapper' "$wrapper"; then
    echo 'Refusing to replace an existing administrator udev override' >&2
    exit 1
fi
cat > /etc/initramfs-tools/scripts/init-top/gpu-unlock-early <<'EARLY'
#!/bin/sh
PREREQ="blacklist"
prereqs() { echo "$PREREQ"; }
case "${1:-}" in prereqs) prereqs; exit 0;; esac
case " $(cat /proc/cmdline) " in
    *' gpu_unlock.targets='*|*' gpu-unlock.targets='*) ;;
    *) exit 0;;
esac
echo '<6>gpu_unlock early: modprobe before init-top/udev coldplug' > /dev/kmsg
if ! modprobe gpu_unlock; then
    echo '<3>gpu_unlock early: modprobe failed; continuing normal binding' > /dev/kmsg
    exit 0
fi
echo '<6>gpu_unlock early: modprobe complete; init-top/udev may start' > /dev/kmsg
EARLY
cat > "$wrapper" <<'UDEV'
#!/bin/sh
# gpu-unlock site wrapper: vendor content is copied fresh into the image by the build hook.
case "${1:-}" in
    prereqs)
        /usr/share/initramfs-tools/scripts/init-top/udev prereqs
        echo gpu-unlock-early
        exit 0
        ;;
esac
exec /lib/gpu-unlock-initramfs/udev "$@"
UDEV
cat > /etc/initramfs-tools/hooks/zz-gpu-unlock-early <<'HOOK'
#!/bin/sh
set -eu
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "${1:-}" in prereqs) prereqs; exit 0;; esac
. /usr/share/initramfs-tools/hook-functions
manual_add_modules gpu_unlock
# No persistent copy of vendor code: every image captures its current packaged udev.
install -d -m 755 "$DESTDIR/lib/gpu-unlock-initramfs"
cp -p /usr/share/initramfs-tools/scripts/init-top/udev "$DESTDIR/lib/gpu-unlock-initramfs/udev"
[ -x "$DESTDIR/lib/gpu-unlock-initramfs/udev" ]
grep -q 'gpu-unlock site wrapper' "$DESTDIR/scripts/init-top/udev"
[ -x "$DESTDIR/scripts/init-top/gpu-unlock-early" ]
HOOK
chmod 755 /etc/initramfs-tools/hooks/zz-gpu-unlock-early /etc/initramfs-tools/scripts/init-top/gpu-unlock-early "$wrapper"
echo 'Installed site wrapper and early hook. Rebuild and inspect actual initramfs ORDER before reboot.'
