#!/bin/bash
# One-time migration from the shared /usr/local fork to separate installations.
# Run make build first. This does not move or delete any container data.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

fail() { printf '%s\n' "$*" >&2; exit 1; }
project=$(cd "$(dirname "$0")/.." && pwd -P)
check_only=false
if [ "${1:-}" = --check ]; then check_only=true; shift; fi
[ "$#" -eq 2 ] || fail "Usage: $0 [--check] FORK.pkg APPLE-1.6.1.pkg"
fork_package=$1
apple_package=$2
runtime='/Library/Application Support/MyBox/Runtime'
helpers='/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service'

for package in "$fork_package" "$apple_package"; do
    [[ "$package" = /* ]] || fail 'Package paths must be absolute.'
    [ -f "$package" ] || fail "Package missing: $package"
done
for program in mybox mybox-worker mybox-service mybox-access mybox-setup mybox-authenticate mybox-firewall-manager mybox-port-forwarder; do
    [ -x "$project/.build/$program" ] || fail 'Run make build before migration.'
done
/bin/sh -n "$project/my-container"

# Pin the Apple release that was checked with the fork. Do not accept an
# arbitrary package when restoring the original command.
expected=cd47846819f06478d067060efe21e516b562ac0a9af8eecad6d5fea0b556b200
actual=$(/usr/bin/shasum -a 256 "$apple_package")
[ "${actual%% *}" = "$expected" ] || fail 'The Apple installer checksum does not match release 1.6.1.'
/usr/sbin/pkgutil --check-signature "$apple_package"
/usr/sbin/spctl --assess --type install "$apple_package"
temporary=$(/usr/bin/mktemp -d /private/tmp/mybox-runtime-package.XXXXXX)
trap 'rm -rf "$temporary"' EXIT
/usr/sbin/pkgutil --expand "$fork_package" "$temporary/fork"
info="$temporary/fork/PackageInfo"
attribute() { /usr/bin/xmllint --xpath "string(/pkg-info/@$1)" "$info"; }
[ "$(attribute identifier)" = io.github.janpmz.my-container-box.runtime ] || fail 'Unexpected fork package identifier.'
[ "$(attribute install-location)" = "$runtime" ] || fail 'The fork package does not use the private installation directory.'
[ "$(attribute version)" = 1.6.1.5 ] || fail 'This migration requires the tested 1.6.1-mybox.5 package.'
if $check_only; then
    printf '%s\n' 'Package checks passed. No installation changes were made.'
    exit 0
fi

[ "$EUID" -eq 0 ] && [[ "${SUDO_UID:-}" =~ ^[1-9][0-9]*$ ]] || fail 'Run this script with sudo from your normal Mac account.'
owner=$SUDO_UID
[ "$(cat "$helpers/owner")" = "$owner" ] || fail 'Run migration from the account that owns MyBox.'
case "$(/usr/local/bin/container --version)" in
    'container CLI version '*'-mybox.'*) ;;
    *) fail 'This migration is only for the old fork installed in /usr/local.' ;;
esac
for label in com.apple.container.apiserver io.github.janpmz.mybox.runtime.apiserver; do
    if /bin/launchctl print "user/$owner/$label" >/dev/null 2>&1; then
        fail "Stop the personal container service before migration: $label"
    fi
done
[ ! -e /usr/local/bin/my-container ] && [ ! -L /usr/local/bin/my-container ] || fail '/usr/local/bin/my-container already exists; inspect it before migration.'
as_owner() { /bin/launchctl asuser "$owner" /usr/bin/sudo -n -H -u "#$owner" -- "$@"; }

# Apple cannot load the fork's reserved-on-demand networks, even when their
# containers were moved to the private account earlier. Detect saved personal
# state before replacing the old executable; empty-store tests do not cover it.
as_owner /bin/bash -c '
    shopt -s nullglob
    for configuration in "$HOME/Library/Application Support/com.apple.container/networks/"*/entity.json; do
        variant=$(/usr/bin/plutil -extract options.variant raw -o - "$configuration" 2>/dev/null || true)
        if [ "$variant" = reserved-on-demand ]; then
            printf "Fork-only personal network requires migration: %s\n" "$configuration" >&2
            exit 1
        fi
    done
' || fail 'Move unused fork network definitions into a backup, or migrate their containers to the fork, before restoring Apple container.'

# A backup of program files and launch jobs supports manual recovery. Box disks
# stay in their existing location and are never included in removal operations.
umask 077
backup=$(/usr/bin/mktemp -d /private/var/db/mybox-runtime-backup.XXXXXX)
printf 'Saving existing programs in %s\n' "$backup"
trap 'status=$?; rm -rf "$temporary"; if [ "$status" -ne 0 ]; then printf "Migration stopped. Existing program backup: %s\n" "$backup" >&2; fi' EXIT
mkdir "$backup/bin" "$backup/launch-jobs"
for file in container container-apiserver uninstall-container.sh; do
    if [ -e "/usr/local/bin/$file" ]; then /bin/cp -cRp "/usr/local/bin/$file" "$backup/bin/"; fi
done
/bin/cp -cRp /usr/local/libexec/container "$backup/libexec-container"
/bin/cp -cRp "$helpers" "$backup/helpers"
for file in /Library/LaunchDaemons/io.github.janpmz.my-container-box.*.plist; do
    if [ -f "$file" ]; then /bin/cp -cRp "$file" "$backup/launch-jobs/"; fi
done
/usr/sbin/pkgutil --pkg-info-plist io.github.janpmz.my-container-box > "$backup/old-receipt.plist"
as_owner "$project/.build/mybox" list --json > "$backup/boxes-before.json"

# Setup checks that all private boxes are stopped and unregisters only the old
# private runtime. It keeps the private account's existing data directory.
/usr/sbin/installer -pkg "$fork_package" -target /
"$project/.build/mybox-setup"
as_owner "$project/.build/mybox" list --json > "$backup/boxes-after.json"
/usr/bin/cmp "$backup/boxes-before.json" "$backup/boxes-after.json" || fail 'Box inventory changed unexpectedly. Apple installation has not been replaced.'

/usr/sbin/installer -pkg "$apple_package" -target /
case "$(/usr/local/bin/container --version)" in
    'container CLI version 1.6.1 ('*) ;;
    *) fail 'The original Apple command did not report the expected version.' ;;
esac
# Apple does not ship this fork-only helper. Remove only the backed-up copy.
bridge=/usr/local/libexec/container/container-mybox-bridge
if [ -f "$bridge" ] && /usr/bin/cmp -s "$bridge" "$backup/libexec-container/container-mybox-bridge"; then
    /bin/rm "$bridge"
fi
/usr/sbin/pkgutil --forget io.github.janpmz.my-container-box
/usr/bin/install -o root -g wheel -m 755 "$project/my-container" /usr/local/bin/my-container
/usr/local/bin/container --version
/usr/local/bin/my-container --version
as_owner "$project/.build/mybox" list --json
printf 'Migration completed. Existing program backup: %s\n' "$backup"
