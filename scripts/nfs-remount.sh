#!/bin/bash
# Apply the standard NFS client mount options to every NFS entry in /etc/fstab and remount them.
#
# usage: sudo scripts/nfs-remount.sh [--dry-run | --remount-later]
#   --dry-run        show the fstab changes and what would be remounted; change nothing
#   --remount-later  rewrite /etc/fstab only and leave the current mounts alone (safe while tasks run);
#                    the options take effect at the next reboot or a later run without this flag. A
#                    mount remounted alone before that gets 1 MiB and hard, but joins the server's
#                    existing connections, so nconnect still waits for all its mounts to be remounted.
#
# Standard options: see OPTS below and docs/03 (NFS client mount options). nconnect is per NFS
# server, not per mount: all mounts of one server on a node share one set of TCP connections, set
# up by the first of them that is mounted. So the script unmounts ALL mounts of a server before
# mounting them again; a server with a busy mount (a process has a file or its cwd there) is
# skipped as a whole and reported. Its fstab lines are still rewritten and take effect at the next
# reboot or rerun.
#
# Refuses while Determined task containers run (FORCE=1 overrides; not checked with --remount-later):
# disable the agent first (det agent disable --drain <agent>) and wait until no task runs on the node.
# Writes a backup of /etc/fstab to /etc/fstab.nfs-remount-<timestamp>.
set -euo pipefail

OPTS="defaults,vers=3,noatime,hard,nconnect=16,rsize=1048576,wsize=1048576,_netdev"
DRY=0
LATER=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY=1 ;;
    --remount-later) LATER=1 ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 1 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

if [ "$LATER" = 0 ] && command -v docker >/dev/null && [ "${FORCE:-0}" != 1 ]; then
    tasks=$(docker ps -q --filter label=ai.determined.container.description)
    if [ -n "$tasks" ]; then
        echo "Determined task containers are running; disable the agent and wait (FORCE=1 overrides):"
        docker ps --filter label=ai.determined.container.description --format '  {{.Names}} {{.Label "ai.determined.container.description"}}'
        exit 1
    fi
fi

# New fstab: NFS lines get the standard options; everything else stays byte for byte.
new=$(mktemp)
awk -v opts="$OPTS" '
    $0 !~ /^[[:space:]]*#/ && ($3 == "nfs" || $3 == "nfs4") {
        $3 = "nfs"; $4 = opts; if ($5 == "") $5 = 0; if ($6 == "") $6 = 0
        print; next
    }
    { print }' /etc/fstab >"$new"

echo "== /etc/fstab changes"
if cmp -s /etc/fstab "$new"; then
    echo "  none (all NFS entries already use the standard options)"
else
    { diff -u /etc/fstab "$new" || true; } | sed -n '3,$p' | grep '^[-+]' || true
    echo "  $( { diff /etc/fstab "$new" || true; } | grep -c '^>') NFS lines change"
fi

# NFS mount points from the new fstab, grouped by server.
declare -A targets
while read -r src tgt; do
    srv=${src%%:*}
    targets[$srv]+="$tgt "
done < <(awk '$0 !~ /^[[:space:]]*#/ && $3 == "nfs" {print $1, $2}' "$new")

if [ "$DRY" = 1 ]; then
    for srv in "${!targets[@]}"; do
        busy=""
        for t in ${targets[$srv]}; do
            mountpoint -q "$t" && fuser -sm "$t" 2>/dev/null && busy+="$t "
        done
        echo "== $srv: $(wc -w <<<"${targets[$srv]}") mounts; ${busy:+busy: $busy}${busy:-none busy: would remount}"
    done
    rm -f "$new"
    exit 0
fi

if cmp -s /etc/fstab "$new"; then
    rm -f "$new"
    [ "$LATER" = 1 ] && { echo "== nothing to write"; exit 0; }
else
    backup=/etc/fstab.nfs-remount-$(date +%Y%m%d-%H%M%S)
    cp -p /etc/fstab "$backup"
    cat "$new" >/etc/fstab
    rm -f "$new"
    systemctl daemon-reload
    echo "== fstab written (backup: $backup)"
fi

if [ "$LATER" = 1 ]; then
    for srv in "${!targets[@]}"; do
        echo "== $srv: $(wc -w <<<"${targets[$srv]}") mounts left as they are; the new options apply at the next reboot or a run without --remount-later"
    done
    exit 0
fi

rc=0
for srv in "${!targets[@]}"; do
    busy=""
    for t in ${targets[$srv]}; do
        if mountpoint -q "$t" && fuser -sm "$t" 2>/dev/null; then
            busy+="$t($(fuser -m "$t" 2>/dev/null | tr -s ' ' | sed 's/^ //')) "
        fi
    done
    if [ -n "$busy" ]; then
        echo "== $srv: SKIPPED, mounts in use: $busy"
        rc=1
        continue
    fi
    for t in ${targets[$srv]}; do
        if mountpoint -q "$t"; then umount "$t"; fi
    done
    failed=""
    for t in ${targets[$srv]}; do
        mkdir -p "$t"
        mount "$t" || failed+="$t "
    done
    # Check the options the kernel applied and the number of transports of the shared client.
    first=$(awk -v s="$srv" '$1 ~ "^"s":" && $3 ~ /^nfs/ {print $2; exit}' /proc/mounts)
    opts=$(awk -v t="$first" '$2 == t {print $4}' /proc/mounts)
    xprts=$(awk -v t="$first" '$0 ~ "mounted on "t" with fstype" {m = 1; next} m && /^device/ {m = 0} m && /xprt:/ {n++} END {print n + 0}' /proc/self/mountstats)
    ok=yes
    for want in nconnect=16 rsize=1048576 wsize=1048576 hard; do
        grep -q "$want" <<<"$opts" || ok="no ($want missing)"
    done
    echo "== $srv: remounted $(wc -w <<<"${targets[$srv]}"), ${failed:+FAILED: $failed, }options ok: $ok, transports: $xprts"
    [ -z "$failed" ] && [ "$ok" = yes ] || rc=1
done
exit $rc
