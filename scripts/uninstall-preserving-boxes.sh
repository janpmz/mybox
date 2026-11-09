#!/bin/bash
# Remove installed programs for an installer test. Box data and its private
# account remain in place. Program files are archived for manual recovery.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
umask 077
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$#" -eq 0 ] || fail "Usage: sudo $0"
[ "$EUID" -eq 0 ] && [[ "${SUDO_UID:-}" =~ ^[1-9][0-9]*$ ]] || fail 'Run this script with sudo from the Mac account that owns MyBox.'
project=$(cd "$(dirname "$0")/.." && pwd -P)
. "$project/scripts/uninstall-support.sh"
helpers='/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service'
runtime='/Library/Application Support/MyBox/Runtime'
data='/private/var/db/mybox-runtime'
owner=$SUDO_UID
app='/Applications/MyBox.app'
if [ -e "$app" ] || [ -L "$app" ]; then
    [ -d "$app" ] && [ ! -L "$app" ] || fail 'Unexpected file type at /Applications/MyBox.app.'
    [ "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$app/Contents/Info.plist")" = io.github.janpmz.mybox.desktop ] || fail 'The application at /Applications/MyBox.app is not MyBox.'
fi
[ "$(cat "$helpers/owner")" = "$owner" ] || fail 'Run this from the Mac account that owns MyBox.'
[ -x "$project/.build/mybox-setup" ] && [ -x "$project/.build/mybox" ] || fail 'The compiled MyBox setup and CLI programs are missing.'
[ -d "$data" ] && [ ! -L "$data" ] || fail 'The saved box directory is missing or is a symlink.'
private_uid=$(/usr/bin/id -u _mybox)
[ "$(/usr/bin/stat -f %u "$data")" = "$private_uid" ] || fail 'Unexpected owner of the saved box directory.'
as_owner() { /bin/launchctl asuser "$owner" /usr/bin/sudo -n -H -u "#$owner" -- "$@"; }

# All deletions below are moves into this root-only program archive. No command
# removes the private home, any container/image store, or account records.
backup=$(/usr/bin/mktemp -d /private/var/db/mybox-uninstall-backup.XXXXXX)
printf 'Archiving installed programs in %s\n' "$backup"
trap 'status=$?; if [ "$status" -ne 0 ]; then printf "Uninstall stopped. Saved box data was not removed. Program archive: %s\n" "$backup" >&2; fi' EXIT
mkdir "$backup/launch-jobs" "$backup/programs" "$backup/receipts"
cp -cRp "$helpers" "$backup/helpers-before"
if [ -d /private/var/db/mybox ]; then cp -cRp /private/var/db/mybox "$backup/firewall-before"; fi
if /bin/launchctl print "user/$private_uid/io.github.janpmz.my-container-box.service" >/dev/null 2>&1; then
    as_owner "$project/.build/mybox" list --json > "$backup/boxes-before.json"
else
    printf '%s\n' 'The private service is already stopped. Resuming cleanup with the saved box files in place.'
fi
if [ -f /usr/local/bin/container ]; then
    /usr/bin/shasum -a 256 /usr/local/bin/container /usr/local/bin/container-apiserver > "$backup/apple-programs.sha256"
fi

# This native preflight verifies the owner and runtime paths, refuses running
# boxes/personal fork services, then stops only the private broker and runtime.
# Quit the app and stop the personal my-container service before invoking this.
"$project/.build/mybox-setup" --package-preflight /

bootout_if_loaded() {
    if /bin/launchctl print "$1" >/dev/null 2>&1; then /bin/launchctl bootout "$1"; fi
    if /bin/launchctl print "$1" >/dev/null 2>&1; then fail "Service did not stop: $1"; fi
}
for suffix in bootstrap approve-access verify-boot firewall; do
    bootout_if_loaded "system/io.github.janpmz.my-container-box.$suffix"
done
bootout_if_loaded "user/$private_uid/io.github.janpmz.my-container-box.service"

# launchctl can return before every plugin exits. Wait up to ten seconds and
# record the exact executable paths if shutdown has not finished.
if ! mybox_wait_for_runtime_exit "$runtime" 2> "$backup/remaining-runtime-processes.txt"; then
    cat "$backup/remaining-runtime-processes.txt" >&2
    fail 'MyBox runtime shutdown has not completed. Programs were left in place; this command can be retried.'
fi

# Record file identities after shutdown. This avoids reading huge sparse disks.
inventory() {
    /usr/bin/find "$data" -type f -exec /usr/bin/stat -f '%i %z %m %u %g %N' {} + | /usr/bin/sort
}
inventory > "$backup/box-files-before.txt"

# Clear only MyBox's PF anchor and release only its own enable reference. Never
# flush the main rules, disable PF globally, or change Apple's container jobs.
/sbin/pfctl -a com.apple/000.mybox -F rules
state=/private/var/db/mybox/state.json
if [ -f "$state" ]; then
    token=$(/usr/bin/plutil -extract token raw -o - "$state")
    saved_boot=$(/usr/bin/plutil -extract boot raw -o - "$state")
    current_boot=$(/usr/sbin/sysctl -n kern.boottime)
    if [[ "$token" =~ ^[0-9]+$ ]] && [ "$saved_boot" = "$current_boot" ]; then
        /sbin/pfctl -s References > "$backup/pf-references-before.txt"
        if /usr/bin/grep -E "(^|[^0-9])$token([^0-9]|$)" "$backup/pf-references-before.txt" >/dev/null; then
            /sbin/pfctl -X "$token"
        fi
    fi
fi

for suffix in bootstrap approve-access verify-boot firewall; do
    file="/Library/LaunchDaemons/io.github.janpmz.my-container-box.$suffix.plist"
    if [ -f "$file" ]; then mv "$file" "$backup/launch-jobs/"; fi
done
for name in mybox-authenticate mybox-worker mybox-port-forwarder mybox-service mybox-access mybox-setup service.plist package-install-owner; do
    if [ -e "$helpers/$name" ]; then mv "$helpers/$name" "$backup/programs/"; fi
done
if [ -f /Library/PrivilegedHelperTools/mybox-firewall-manager ]; then
    mv /Library/PrivilegedHelperTools/mybox-firewall-manager "$backup/programs/"
fi
if [ -d "$runtime" ]; then mv "$runtime" "$backup/Runtime"; fi
if [ -d "$app" ]; then mv "$app" "$backup/MyBox.app"; fi
for name in mybox my-container; do
    if [ -e "/usr/local/bin/$name" ] || [ -L "/usr/local/bin/$name" ]; then
        mv "/usr/local/bin/$name" "$backup/programs/"
    fi
done
for receipt in io.github.janpmz.mybox io.github.janpmz.my-container-box.runtime; do
    if /usr/sbin/pkgutil --pkg-info "$receipt" >/dev/null 2>&1; then
        /usr/sbin/pkgutil --pkg-info-plist "$receipt" > "$backup/receipts/$receipt.plist"
        /usr/sbin/pkgutil --files "$receipt" > "$backup/receipts/$receipt-files.txt"
        /usr/sbin/pkgutil --forget "$receipt"
    fi
done

inventory > "$backup/box-files-after.txt"
/usr/bin/cmp "$backup/box-files-before.txt" "$backup/box-files-after.txt" || fail 'Saved box file metadata changed; inspect the archive before testing the installer.'
for name in owner identity.plist; do /usr/bin/cmp "$helpers/$name" "$backup/helpers-before/$name"; done
[ "$(/usr/bin/id -u _mybox)" = "$private_uid" ] || fail 'The private account identity changed.'
if [ -f "$backup/apple-programs.sha256" ]; then /usr/bin/shasum -a 256 -c "$backup/apple-programs.sha256"; fi
printf '\nMyBox programs and services are uninstalled. Box files, settings, and the private account were preserved.\n'
printf 'Program archive: %s\n' "$backup"
printf 'Ready to install: %s/dist/MyBox-0.1.1-arm64.pkg\n' "$project"
