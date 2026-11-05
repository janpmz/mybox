import Darwin
import Foundation

// This is a one-shot sudo operation. The broker supplies the policy. The
// terminal supplies only the random request number, never the policy or result.
func approveServiceFirewall(_ token: String) throws {
    try requireFirewallAdministrator()
    let caller = try String(contentsOfFile: serviceInstall + "/owner", encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard UInt32(caller) != nil, ProcessInfo.processInfo.environment["SUDO_UID"] == caller else {
        throw BoxError("Only the configured Mac account can approve a MyBox firewall request.")
    }
    let uid = try privateAccountID()
    let home = try serviceDirectory(AT_FDCWD, serviceHome, owner: uid)
    defer { close(home) }
    let pending = try serviceDirectory(home, "requests", owner: uid)
    defer { close(pending) }
    let dir = try serviceDirectory(pending, try serviceRequestToken(token), owner: uid)
    defer { close(dir) }
    let fd = openat(dir, "request", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw BoxError("The firewall request expired.") }
    let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    defer { try? file.close() }
    var info = stat()
    guard fstat(fd, &info) == 0, info.st_uid == uid, info.st_mode & S_IFMT == S_IFREG,
          info.st_nlink == 1, info.st_size > 0, info.st_size <= 8192 else {
        throw BoxError("Invalid firewall request file.")
    }
    // Claim the request before doing privileged work. A second sudo invocation
    // cannot replay it after the box starts or its firewall configuration changes.
    let claim = openat(dir, "claimed", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o400)
    guard claim >= 0 else { throw BoxError("This firewall request was already used.") }
    close(claim)
    let request = try JSONDecoder().decode(ServiceFirewallRequest.self, from: file.readData(ofLength: 8193))
    guard ["apply", "verify", "update-ports", "remove"].contains(request.action), request.policy.owner == uid else {
        throw BoxError("Unsupported firewall request.")
    }
    let json = String(decoding: try JSONEncoder().encode(request.policy), as: UTF8.self)
    try firewallMain([request.action, json], serviceOwner: uid) {
        // Check after taking the firewall lock. A late approval cannot apply
        // an abandoned request after a newer operation restores protection.
        guard flock(fd, LOCK_EX | LOCK_NB) == -1, errno == EWOULDBLOCK else {
            throw BoxError("The box operation ended before firewall approval.")
        }
        if request.action != "remove" {
            try checkFirewallWatcher()
        }
    }
    let receipt = openat(dir, "approved", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o444)
    guard receipt >= 0 else { throw BoxError("Cannot confirm the firewall operation.") }
    close(receipt)
}

func checkFirewallWatcher() throws {
    let watcher = open(firewallURL("watch.lock").path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    defer { if watcher >= 0 { close(watcher) } }
    guard watcher >= 0, flock(watcher, LOCK_SH | LOCK_NB) == -1, errno == EWOULDBLOCK else {
        throw BoxError("The Mac firewall watcher must be running before MyBox can continue.")
    }
    let report = try JSONDecoder().decode(FirewallWatcherStatus.self, from: Data(contentsOf: firewallURL("watcher.json")))
    let age = Date().timeIntervalSince1970 - report.lastCheck
    guard (0...20).contains(age), report.error == nil else {
        throw BoxError("The Mac firewall watcher has not confirmed a recent successful check.")
    }
}

// launchd passes the connected private-account socket on stdin and stdout.
// Reply with one byte only after checking the live PF anchor under the firewall lock.
func verifyRuntimeBoot() throws {
    try requireFirewallAdministrator()
    let owner = try privateAccountID()
    var uid = uid_t.max, gid = gid_t.max
    guard getpeereid(STDIN_FILENO, &uid, &gid) == 0, uid == owner else {
        throw BoxError("Only the private MyBox runtime can request a boot check.")
    }
    var timeout = timeval(tv_sec: 3, tv_usec: 0)
    guard setsockopt(STDIN_FILENO, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout))) == 0 else {
        throw BoxError("Cannot set the boot request timeout.")
    }
    var bytes = Data()
    while bytes.count <= 1024 {
        var buffer = [UInt8](repeating: 0, count: 1025 - bytes.count)
        let count = read(STDIN_FILENO, &buffer, buffer.count)
        if count == 0 { break }
        guard count > 0 else { throw BoxError("Incomplete runtime boot request.") }
        bytes.append(contentsOf: buffer.prefix(count))
    }
    guard bytes.count <= 1024 else { throw BoxError("Runtime boot request is too large.") }
    let request = try JSONDecoder().decode([String: String].self, from: bytes)
    guard let box = request["box"], matches(box, "^mybox-[a-z][a-z0-9-]{0,47}$"),
          request["network"] == "mybox-net-" + box.dropFirst(6) else {
        throw BoxError("The runtime requested an unexpected box network.")
    }
    let lock = try FileLock(firewallDirectory + "/lock")
    defer { withExtendedLifetime(lock) {} }
    try checkFirewallWatcher()
    var store = try readFirewallStore()
    guard let policy = store.policies.first(where: { $0.owner == owner && $0.network == request["network"] }),
          policy.active, policy.gateway == request["gateway"], policy.subnet4 == request["subnet4"],
          policy.subnet6 == request["subnet6"] else { throw BoxError("No active firewall policy matches this box and network.") }
    let bridge = try policy.bridge(in: hostInterfaces())
    _ = try reconcileFirewall(&store)
    guard try policy.bridge(in: hostInterfaces()) == bridge else { throw BoxError("The boot network bridge changed.") }
    try FileHandle.standardOutput.write(contentsOf: Data([1]))
}
