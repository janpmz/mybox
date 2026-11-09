#!/bin/bash
# Developer build tool. Nothing in this script installs or starts a service.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
project=$(cd "$(dirname "$0")/.." && pwd -P)
runtime_package=${1:-"$project/../mybox-container/bin/release/my-container-box-installer.pkg"}
runtime_source=$(cd "$project/../mybox-container" && pwd -P)
[ -f "$runtime_package" ] || { printf 'Build the fork release package first: %s\n' "$runtime_package" >&2; exit 1; }
sign=${MYBOX_SIGN:-0}
notarize=${MYBOX_NOTARIZE:-0}
[[ "$sign" =~ ^[01]$ && "$notarize" =~ ^[01]$ ]] || { printf 'MYBOX_SIGN and MYBOX_NOTARIZE must be 0 or 1.\n' >&2; exit 1; }
[[ "$notarize" == 0 || "$sign" == 1 ]] || { printf 'Notarization requires Developer ID signing.\n' >&2; exit 1; }
mkdir -p "$project/.build" "$project/dist"
work=$(mktemp -d "$project/.build/package.XXXXXX")
source="$work/source"
payload="$work/payload"
scripts="$work/scripts"
printf 'Package build directory: %s\n' "$work"
mkdir -p "$source" "$payload" "$scripts" "$work/components"
signing_options=(--config "$work/signing.json")
if [ "$notarize" = 1 ]; then signing_options+=(--notarize); fi
if [ "$sign" = 1 ]; then
    # Check both certificates before spending time compiling. Signing errors
    # must never quietly produce an unsigned release package.
    python3 "$project/packaging/signing.py" configure "${signing_options[@]}"
fi

# Snapshot source files so another conversation's UI build cannot overwrite the
# app that is being packaged. Existing build caches and development media stay out.
for directory in Sources Shared service desktop packaging; do
    /usr/bin/rsync -a --exclude .build --exclude .DS_Store "$project/$directory" "$source/"
done
cp "$project/Makefile" "$project/my-container" "$source/"
make -C "$source" -j 2 build
make -C "$source/desktop" build test

/usr/sbin/pkgutil --expand-full "$runtime_package" "$work/runtime"
info="$work/runtime/PackageInfo"
[ "$(/usr/bin/xmllint --xpath 'string(/pkg-info/@identifier)' "$info")" = io.github.janpmz.my-container-box.runtime ]
[ "$(/usr/bin/xmllint --xpath 'string(/pkg-info/@install-location)' "$info")" = '/Library/Application Support/MyBox/Runtime' ]
runtime="$payload/Library/Application Support/MyBox/Runtime"
helpers="$payload/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service"
mkdir -p "$payload/Applications" "$payload/usr/local/bin" "$runtime" "$helpers"
/usr/bin/ditto --norsrc --noextattr "$work/runtime/Payload" "$runtime"
python3 "$source/packaging/prepare-runtime.py" "$runtime"
# The standalone runtime uninstaller does not own the combined package.
rm -f "$runtime/bin/uninstall-my-container.sh"
mkdir -p "$runtime/legal"
cp "$runtime_source/LICENSE" "$runtime/legal/container-LICENSE.txt"
/usr/bin/ditto --norsrc --noextattr "$source/desktop/.build/MyBox.app" "$payload/Applications/MyBox.app"
app="$payload/Applications/MyBox.app"
printf '%s\n' /usr/local/bin/mybox > "$app/Contents/Resources/cli-location.txt"
/usr/libexec/PlistBuddy -c 'Set :LSMinimumSystemVersion 26.0' "$app/Contents/Info.plist"
/usr/bin/codesign --force --sign - --identifier io.github.janpmz.mybox.desktop "$app"
/usr/bin/install -m 755 "$source/.build/mybox" "$payload/usr/local/bin/mybox"
/usr/bin/install -m 755 "$source/my-container" "$payload/usr/local/bin/my-container"
for name in mybox-authenticate mybox-worker mybox-port-forwarder mybox-service mybox-access mybox-setup; do
    /usr/bin/install -m 755 "$source/.build/$name" "$helpers/$name"
done
/usr/bin/install -m 755 "$source/.build/mybox-firewall-manager" "$payload/Library/PrivilegedHelperTools/mybox-firewall-manager"
/usr/bin/install -m 755 "$source/packaging/scripts/preinstall" "$scripts/preinstall"
/usr/bin/install -m 755 "$source/packaging/scripts/postinstall" "$scripts/postinstall"
/usr/bin/install -m 755 "$source/.build/mybox-setup" "$scripts/mybox-setup"
if [ "$sign" = 1 ]; then
    python3 "$source/packaging/signing.py" payload --config "$work/signing.json" --payload "$payload" --scripts "$scripts"
fi

# pkgbuild records root ownership without requiring sudo for this build.
/usr/bin/pkgbuild --root "$payload" --identifier io.github.janpmz.mybox --version 0.1.0 \
    --install-location / --ownership recommended --scripts "$scripts" \
    --component-plist "$source/packaging/components.plist" "$work/components/MyBox-component.pkg"
/usr/bin/productbuild --distribution "$source/packaging/Distribution.xml" \
    --resources "$source/packaging/resources" --package-path "$work/components" "$work/MyBox-0.1.0-arm64.pkg"
if [ "$sign" = 1 ]; then
    python3 "$source/packaging/signing.py" package "${signing_options[@]}" \
        --package "$work/MyBox-0.1.0-arm64.pkg"
fi
python3 "$project/tests/package_layout.py" "$work/MyBox-0.1.0-arm64.pkg" --report "$work/verification.json"

# Publish only a package that passed inspection; a failed build keeps the prior
# package intact. Record the exact source snapshot used for the UI too.
cp "$work/MyBox-0.1.0-arm64.pkg" "$project/dist/MyBox-0.1.0-arm64.pkg"
cp "$work/verification.json" "$project/dist/MyBox-0.1.0-verification.json"
python3 "$project/packaging/write-build-manifest.py" "$project" "$source" "$runtime_package" "$runtime_source" "$work"
(cd "$project/dist" && /usr/bin/shasum -a 256 MyBox-0.1.0-arm64.pkg > MyBox-0.1.0-arm64.pkg.sha256)
cp "$source/packaging/TESTING.txt" "$project/dist/TESTING.txt"
for report in notarization-submission.json notarization-log.json; do
    if [ -f "$work/$report" ]; then
        cp "$work/$report" "$project/dist/MyBox-0.1.0-$report"
    else
        rm -f "$project/dist/MyBox-0.1.0-$report"
    fi
done
printf '\nPackage ready: %s\n' "$project/dist/MyBox-0.1.0-arm64.pkg"
