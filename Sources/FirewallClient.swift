import Foundation
import Darwin

func installedFirewall() throws {
    #if MYBOX_TESTING
    if ProcessInfo.processInfo.environment["MYBOX_TEST_HOST"] != nil { return }
    #endif
    var info = stat()
    guard lstat(firewallExecutable, &info) == 0, info.st_uid == 0,
          info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_mode & 0o022 == 0 else {
        throw BoxError("Run mybox setup-network once in a Mac terminal before starting a box.")
    }
}

func firewallRequest(_ action: String, policy: NetworkPolicy) throws {
    try installedFirewall()
    #if MYBOX_SERVICE_WORKER
    try requestServiceApproval(action, policy: policy)
    #else
    let json = String(decoding: try JSONEncoder().encode(policy), as: UTF8.self)
    let result = try hostCommand("/usr/bin/sudo", [firewallExecutable, action, json], terminal: true)
    guard result.output.trimmingCharacters(in: .whitespacesAndNewlines) == "mybox-firewall-manager-\(firewallVersion)-ok" else {
        throw BoxError("The firewall helper did not confirm protection. Run mybox setup-network to update it.")
    }
    #endif
}

#if MYBOX_SERVICE_WORKER
private func requestServiceApproval(_ action: String, policy: NetworkPolicy) throws {
    guard getuid() == (try privateAccountID()),
          let channel = ProcessInfo.processInfo.environment["MYBOX_FIREWALL_CHANNEL"].flatMap(Int32.init) else {
        throw BoxError("The private MyBox service must request firewall approval.")
    }
    let token = UUID().uuidString.lowercased()
    let directory = URL(fileURLWithPath: serviceHome + "/requests").appendingPathComponent(token)
    guard mkdir(directory.path, 0o700) == 0 else { throw BoxError("Cannot create the firewall request.") }
    defer { try? files.removeItem(at: directory) }
    let request = ServiceFirewallRequest(action: action, policy: policy)
    try JSONEncoder().encode(request).write(to: directory.appendingPathComponent("request"), options: .atomic)
    let lifetime = try FileLock(directory.appendingPathComponent("request").path)
    defer { withExtendedLifetime(lifetime) {} }
    if let approval = boxAccessApproval {
        // Never fall back to terminal sudo after a denied or expired approval.
        try approval.firewall(token)
    } else {
        let data = Data(token.utf8)
        let sent = data.withUnsafeBytes { send(channel, $0.baseAddress, $0.count, 0) }
        guard sent == data.count else { throw BoxError("Cannot send the firewall request to your terminal.") }
        // A client that keeps its socket open cannot hold the box lock indefinitely.
        var timeout = timeval(tv_sec: 300, tv_usec: 0)
        guard setsockopt(channel, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
            throw BoxError("Cannot set the firewall approval timeout.")
        }
        var answer: UInt8 = 0
        var count: Int
        repeat { count = recv(channel, &answer, 1, 0) } while count < 0 && errno == EINTR
        guard count == 1, answer == Character("Y").asciiValue else { throw BoxError("Firewall approval failed.") }
    }
    // A client reply alone cannot authorize startup. Only the root helper can
    // create a root-owned file in this new, service-private directory.
    var info = stat()
    guard lstat(directory.appendingPathComponent("approved").path, &info) == 0, info.st_uid == 0,
          info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1 else {
        throw BoxError("The Mac firewall helper did not confirm this request.")
    }
    let issued = Double(info.st_mtimespec.tv_sec) + Double(info.st_mtimespec.tv_nsec) / 1_000_000_000
    guard (0...5).contains(Date().timeIntervalSince1970 - issued) else {
        throw BoxError("The firewall confirmation expired. Retry the box command.")
    }
}
#endif
