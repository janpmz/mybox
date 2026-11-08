import Darwin
import Foundation

private let packageOwnerFile = helperDirectory + "/package-install-owner"

private func readPackageFile(_ path: String, mode: mode_t) throws -> Data {
    var metadata = stat()
    guard lstat(path, &metadata) == 0, metadata.st_uid == 0,
          metadata.st_mode & S_IFMT == S_IFREG, metadata.st_mode & 0o7777 == mode,
          metadata.st_nlink == 1, metadata.st_size <= 65536 else {
        throw BoxError("Unexpected owner, permissions, or file type at \(path). Installation stopped.")
    }
    return try Data(contentsOf: URL(fileURLWithPath: path))
}

func packageCommand(_ executable: String, _ arguments: [String]) throws -> ServiceCommandResult {
    do { return try serviceCommand(executable, arguments, timeout: 30) }
    catch is ServiceTimeout { throw BoxError("Installer timed out running \(executable). Stop MyBox and my-container before retrying.") }
}

func selectPackageOwner(existing: uid_t?, console: uid_t?) throws -> uid_t {
    func ordinary(_ uid: uid_t) -> Bool { uid >= 500 && uid < 65534 }
    if let existing {
        guard ordinary(existing), console == nil || console == existing else {
            throw BoxError("Install MyBox from the Mac account that already owns its boxes.")
        }
        return existing
    }
    guard let console, ordinary(console) else {
        throw BoxError("Sign in to your Mac account before installing MyBox.")
    }
    return console
}

func requirePackageStartupDisk(_ target: String) throws {
    guard target.hasPrefix("/"), URL(fileURLWithPath: target).standardizedFileURL.path == "/" else {
        throw BoxError("Install MyBox on the current startup disk.")
    }
}

func requirePackageTarget(_ target: String) throws {
    guard geteuid() == 0 else { throw BoxError("The MyBox package must be run by macOS Installer with administrator authentication.") }
    try requirePackageStartupDisk(target)
    #if !arch(arm64)
    throw BoxError("This MyBox package requires an Apple silicon Mac.")
    #endif
    guard ProcessInfo.processInfo.isOperatingSystemAtLeast(OperatingSystemVersion(majorVersion: 26, minorVersion: 0, patchVersion: 0)) else {
        throw BoxError("This MyBox package requires macOS 26 or later.")
    }
}

private func verifyExistingPackagePaths() throws {
    // Installer may create missing directories, but never follow a pre-existing
    // symlink into a user's writable directory when installing root helpers.
    for path in ["/Library", "/Library/Application Support", "/Library/Application Support/MyBox", forkInstallRoot,
                 "/Library/PrivilegedHelperTools", helperDirectory,
                 forkInstallRoot + "/bin", forkInstallRoot + "/bin/container"] {
        var metadata = stat()
        if lstat(path, &metadata) != 0 {
            guard errno == ENOENT else { throw systemError("Cannot inspect \(path)") }
            continue
        }
        guard metadata.st_uid == 0, metadata.st_mode & S_IFMT != S_IFLNK, metadata.st_mode & 0o022 == 0 else {
            throw BoxError("Unexpected ownership or permissions on \(path). Installation stopped.")
        }
    }
}

private func packageOwner() throws -> uid_t {
    var consoleInfo = stat()
    guard stat("/dev/console", &consoleInfo) == 0 else { throw systemError("Cannot identify the signed-in Mac account") }
    let console: uid_t? = consoleInfo.st_uid >= 500 && consoleInfo.st_uid < 65534 ? consoleInfo.st_uid : nil
    let ownerPath = helperDirectory + "/owner"
    var existing: uid_t?
    if FileManager.default.fileExists(atPath: ownerPath) {
        let data = try readPackageFile(ownerPath, mode: 0o644)
        guard let value = UInt32(String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)) else {
            throw BoxError("The existing MyBox installation has an invalid owner.")
        }
        existing = value
    }
    let owner = try selectPackageOwner(existing: existing, console: console)
    guard getpwuid(owner) != nil else { throw BoxError("The MyBox owner account no longer exists.") }
    return owner
}

func requirePersonalForksStopped(_ users: [uid_t], command: InstallCommand = packageCommand) throws {
    for uid in Set(users).sorted() {
        for domain in ["gui/\(uid)", "user/\(uid)"] {
            if try command("/bin/launchctl", ["print", domain + "/" + forkServicePrefix + "apiserver"]).status == 0 {
                throw BoxError("Run my-container system stop in Mac account \(uid) before installing MyBox. Apple's container may keep running.")
            }
        }
    }
}

// Returns whether a server is present; throws if any box is not stopped.
func checkPackageRuntimeCanStop(_ uid: uid_t, command: InstallCommand = packageCommand,
                                   expectedDataRoot: String = privateServiceHome + "/Library/Application Support/com.apple.container") throws -> Bool {
    if try command("/bin/launchctl", ["print", "user/\(uid)/com.apple.container.apiserver"]).status == 0 {
        throw BoxError("Migrate the older shared MyBox runtime before using this package.")
    }
    guard try command("/bin/launchctl", ["print", "user/\(uid)/" + forkServicePrefix + "apiserver"]).status == 0 else { return false }
    let prefix = ["asuser", String(uid), "/usr/bin/sudo", "-n", "-H", "-u", "#\(uid)", "--", forkInstallRoot + "/bin/container"]
    let health = try checkedInstallCommand("/bin/launchctl", prefix + ["system", "status", "--format", "json"], command: command)
    guard let metadata = try JSONSerialization.jsonObject(with: health.output) as? [String: Any],
          metadata["status"] as? String == "running", let paths = metadata["paths"] as? [String: String],
          let root = paths["appRoot"], let install = paths["installRoot"],
          URL(fileURLWithPath: root).resolvingSymlinksInPath().standardizedFileURL.path == URL(fileURLWithPath: expectedDataRoot).resolvingSymlinksInPath().standardizedFileURL.path,
          URL(fileURLWithPath: install).standardizedFileURL.path == forkInstallRoot else {
        throw BoxError("The private MyBox runtime reports unexpected paths. Installation stopped.")
    }
    let listing = try checkedInstallCommand("/bin/launchctl", prefix + ["list", "--all", "--format", "json"], command: command)
    guard let containers = try JSONSerialization.jsonObject(with: listing.output) as? [[String: Any]],
          containers.allSatisfy({ ($0["status"] as? [String: Any])?["state"] as? String == "stopped" }) else {
        throw BoxError("Stop all MyBox boxes before installing or updating MyBox.")
    }
    return true
}

func preparePackageInstallation(target: String) throws {
    try requirePackageTarget(target)
    try verifyExistingPackagePaths()
    let owner = try packageOwner()
    let app = try packageCommand("/usr/bin/pgrep", ["-x", "MyBox"])
    guard app.status == 1 else { throw BoxError("Quit the MyBox desktop app before installing or updating MyBox.") }
    let users = serviceAccounts().map(\.1).filter { $0 >= 500 && $0 < 65534 }
    try requirePersonalForksStopped(users)

    let privateUID: uid_t?
    if let account = getpwnam(privateServiceAccount) {
        let uid = account.pointee.pw_uid
        let identityPath = helperDirectory + "/identity.plist"
        let identity = try PropertyListDecoder().decode(ServiceIdentity.self, from: readPackageFile(identityPath, mode: 0o600))
        guard (300...499).contains(uid), identity.number == uid, account.pointee.pw_gid == uid,
              String(cString: account.pointee.pw_dir) == privateServiceHome else { throw BoxError("The _mybox account belongs to an unexpected installation.") }
        privateUID = uid
        _ = try checkPackageRuntimeCanStop(uid)
    } else { privateUID = nil }

    // This shared system directory may have the sticky bit. Keep that extra
    // deletion protection; MyBox's own directories still require exact modes.
    try installDirectory("/Library/PrivilegedHelperTools", uid: 0, gid: 0, mode: 0o755, allowStickyBit: true)
    try installDirectory(helperDirectory, uid: 0, gid: 0, mode: 0o755)
    try storeInstalledFile(packageOwnerFile, content: Data("\(owner)\n".utf8), mode: 0o600)
    if let uid = privateUID {
        let service = "user/\(uid)/io.github.janpmz.my-container-box.service"
        let bootstrap = "system/io.github.janpmz.my-container-box.bootstrap"
        do {
            for job in [bootstrap, service] {
                if try packageCommand("/bin/launchctl", ["print", job]).status == 0 {
                    try checkedInstallCommand("/bin/launchctl", ["bootout", job], command: packageCommand)
                }
            }
            // Recheck after closing the broker so new commands cannot race with
            // replacing the runtime. Never ask the runtime to stop running boxes.
            if try checkPackageRuntimeCanStop(uid) {
                let prefix = ["asuser", String(uid), "/usr/bin/sudo", "-n", "-H", "-u", "#\(uid)", "--", forkInstallRoot + "/bin/container"]
                try checkedInstallCommand("/bin/launchctl", prefix + ["system", "stop"], command: packageCommand)
                guard try packageCommand("/bin/launchctl", ["print", "user/\(uid)/" + forkServicePrefix + "apiserver"]).status != 0 else {
                    throw BoxError("The private runtime did not stop. Installation stopped before replacing files.")
                }
            }
        } catch {
            _ = try? packageCommand("/bin/launchctl", ["bootstrap", "system", "/Library/LaunchDaemons/io.github.janpmz.my-container-box.bootstrap.plist"])
            throw error
        }
    }
    print("MyBox package checks passed. Existing box data will be preserved.")
}

func finishPackageInstallation(target: String, artifactDirectory: String) throws {
    try requirePackageTarget(target)
    try verifyExistingPackagePaths()
    let data = try readPackageFile(packageOwnerFile, mode: 0o600)
    guard let owner = UInt32(String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)),
          owner >= 500, owner < 65534, getpwuid(owner) != nil else { throw BoxError("The package did not prepare a valid MyBox owner.") }
    try installMyBoxService(artifactDirectory: artifactDirectory, packageOwner: owner, reloadServices: true)
    guard unlink(packageOwnerFile) == 0 else { throw systemError("Cannot finish package setup") }
    print("MyBox is installed. Open /Applications/MyBox.app or run mybox help.")
}
