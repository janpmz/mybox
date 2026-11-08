# Local MyBox package

`make package` produces `dist/MyBox-0.1.0-arm64.pkg` plus a checksum, build
manifest, verification report, and test instructions. It does not install the
package or change launchd jobs, accounts, firewall rules, or existing boxes.

The fork's release package must already exist at
`../mybox-container/bin/release/my-container-box-installer.pkg`. To supply a
different path, run `./packaging/build-package.sh /absolute/path/runtime.pkg`.
Build the runtime from the fork checkout with:

```sh
make BUILD_CONFIGURATION=release SWIFT_CONFIGURATION_EXTRA='--disable-automatic-resolution --skip-update' build installer-pkg
```

The builder snapshots MyBox's source before compiling, including uncommitted
desktop changes. It builds the app and native helpers inside that snapshot,
replaces the desktop's development CLI reference with `/usr/local/bin/mybox`,
and packages the tested fork. Developer tools and Python are build/test tools
only and are not included in the installed product. `dist` and the build
directories are ignored by Git.

The builder removes development-toolchain library search paths from the staged
fork binaries, then restores their local signatures and entitlements. The
package uses the Swift libraries supplied by macOS 26. Debug, local, and global
symbol tables remain in the installed executables for crash diagnosis and
debugging. The builder verifies that every symbol entry and name, as well as
library dependencies, survives packaging unchanged. All runtime plugins remain
included.

The single mandatory component installs:

| Location | Contents |
| --- | --- |
| `/Applications/MyBox.app` | Desktop app |
| `/usr/local/bin/mybox` | Compiled CLI |
| `/usr/local/bin/my-container` | Launcher for the private fork |
| `/Library/Application Support/MyBox/Runtime` | Fork executable and plugins |
| `/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service` | Swift service, setup, authentication, worker, and forwarding helpers |
| `/Library/PrivilegedHelperTools/mybox-firewall-manager` | Swift firewall manager |

The combined receipt is `io.github.janpmz.mybox`. The native setup program still
accepts the older standalone runtime receipt for source-based development.
The `.app` is not relocatable: Installer must not update the development bundle
inside the source checkout instead of installing in Applications.

## Installer behavior

The welcome screen explains why macOS will ask for Touch ID or an administrator
password. There is no separate Read Me page or component selection. Destination
Select uses Apple's `domains` settings, with only "Install for all users of this
computer" enabled. Home-directory and other-volume installation are disabled
because the private account and system services require the startup disk.
Installer owns the sidebar, installation summary, space
estimate, authentication, progress and completion screens. Native setup also
rejects unsupported target paths before changing any files or services.

Preinstall invokes the package's compiled setup program as root. It verifies the
startup volume, macOS version, existing owner and service paths, and rejects
running desktop or personal fork services. It checks that private MyBox boxes
are stopped before stopping the private broker and runtime. Apple's independent
runtime is never stopped or replaced. A first installation uses the signed-in
Mac account; an update retains the saved owner.

Postinstall invokes the newly installed setup program. It creates or verifies
the private account, configures the firewall and launchd jobs, and waits for the
private broker socket to become available. It does not start Linux or download
images during installation. Program payloads contain no box disks, owner files,
or saved account identities.

If installation fails after preinstall has stopped the private service, retry
the package after correcting the reported error. This test build does not
implement automatic rollback. The existing data directories are preserved.

## Verification and current limits

`make package-test` re-extracts the built package and checks its distribution,
payload, ownership metadata, local executable signatures, architecture, runtime
entitlements, dependencies, and basic commands. It asks macOS Installer to
enumerate supported domains and volumes and verifies that only `LocalSystem` and
`/` are enabled. `tests/test_package_install.py` checks accepted and rejected
destination paths, owner selection, and upgrade decisions using the native Swift
code without administrator access or changes to real services.

Use [TESTING.txt](TESTING.txt) when reviewing the package on a Mac. Actual fresh
installation, upgrade, reboot, and guest/network behavior remain manual tests.
The initial package targets Apple silicon and macOS 26+. The app and binaries
use local ad-hoc signatures; the installer is unsigned and unnotarized. Public
signing, notarization, release publication, and a tested removal workflow are
separate later work.
