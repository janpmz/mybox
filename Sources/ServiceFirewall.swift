import Darwin
import Foundation

let serviceHome = "/private/var/db/mybox-runtime"
let serviceInstall = "/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service"
let serviceAccount = "_mybox"
let forkInstallRoot = "/usr/local"
let forkBridgeExecutable = forkInstallRoot + "/libexec/container/container-mybox-bridge"

struct ServiceFirewallRequest: Codable {
    let action: String
    let policy: NetworkPolicy
}

func privateAccountID() throws -> uid_t {
    guard let entry = getpwnam(serviceAccount), entry.pointee.pw_uid != 0,
          String(cString: entry.pointee.pw_dir) == serviceHome else {
        throw BoxError("The private MyBox account is missing or has an unexpected home directory.")
    }
    return entry.pointee.pw_uid
}

func serviceRequestToken(_ value: String) throws -> String {
    guard UUID(uuidString: value)?.uuidString.lowercased() == value else {
        throw BoxError("Invalid firewall request number.")
    }
    return value
}

func serviceDirectory(_ parent: Int32, _ name: String, owner: uid_t) throws -> Int32 {
    let fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    var info = stat()
    guard fd >= 0, fstat(fd, &info) == 0, info.st_uid == owner, info.st_mode & 0o777 == 0o700 else {
        if fd >= 0 { close(fd) }
        throw BoxError("The firewall request directory has unexpected permissions.")
    }
    return fd
}
