import Darwin
import Foundation

let serviceHome = "/private/var/db/mybox-runtime"
let serviceInstall = "/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service"
let serviceAccount = "_mybox"
let forkBridgeExecutable = forkInstallRoot + "/libexec/container/container-mybox-bridge"

struct ServiceFirewallRequest: Codable {
    let action: String
    let policy: NetworkPolicy
}

// Supplied only by the root approval service over the helper's private stdin.
struct ServiceAccessScope: Codable {
    let box: String
    let expires: TimeInterval
    let actions: [String]
    let policy: NetworkPolicy?

    func validate(_ request: ServiceFirewallRequest, owner: uid_t, now: TimeInterval = Date().timeIntervalSince1970) throws {
        guard expires > now, expires - now <= 301 else { throw BoxError("Box preparation approval expired.") }
        guard matches(box, "^[a-z][a-z0-9-]{0,47}$"),
              request.policy.owner == owner, request.policy.network == "mybox-net-" + box,
              request.policy.active, ["apply", "verify", "update-ports"].contains(request.action),
              actions.contains(request.action), policy == nil || policy == request.policy else {
            throw BoxError("This firewall request is outside the approved box operation.")
        }
        try request.policy.validate()
    }
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
