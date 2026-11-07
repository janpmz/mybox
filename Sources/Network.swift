import Foundation
import Darwin

let networkLabel = "io.github.janpmz.my-container-box.network"
let helperLabel = "io.github.janpmz.my-container-box.helper"
let trustedImage = "docker.io/library/debian:trixie-slim"

func operationLock(_ name: String) throws -> FileLock {
    #if MYBOX_TESTING
    if let path = ProcessInfo.processInfo.environment["MYBOX_TEST_LOG"] { return try FileLock(path + "." + name + ".lock") }
    #endif
    let directory = files.homeDirectoryForCurrentUser.appendingPathComponent(".config/mybox", isDirectory: true)
    try files.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    return try FileLock(directory.appendingPathComponent(name + ".lock").path)
}

func networkLock() throws -> FileLock { try operationLock("network") }

func setupNetwork() throws {
    let manager = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        .deletingLastPathComponent().appendingPathComponent("mybox-firewall-manager").path
    guard files.isExecutableFile(atPath: manager) else {
        throw BoxError("The mybox-firewall-manager executable is missing beside mybox. Run make build.")
    }
    _ = try hostCommand("/usr/bin/sudo", [manager, "install"], terminal: true)
    print("Installed mybox-firewall-manager and updated the Mac startup configuration.")
    print("MyBox will check PF protection before each box starts.")
    print("The firewall watcher refreshes protection automatically when Mac network interfaces change.")
}

extension Backend {
    func unusedBoxSubnets() throws -> (ipv4: String, ipv6: String) {
        let data = try run(["network", "list", "--format", "json"], capture: true)
        guard let networks = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            throw BoxError("Cannot read saved network addresses.")
        }
        let saved = try networks.map { network -> String in
            guard let status = network["status"] as? [String: Any], let subnet = status["ipv4Subnet"] as? String else {
                throw BoxError("A saved network has no IPv4 subnet.")
            }
            return subnet
        }
        let occupied = saved + (try hostInterfaces()).flatMap(\.networks4)
        let ipv4 = try unusedPrivateSubnet(occupied)
        // A locally generated IPv6 /64 is saved with the box, not allocated anew
        // on every start. It consumes no macOS network resources while stopped.
        let hex = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let groups = stride(from: 0, to: 12, by: 4).map { offset in
            String(hex.dropFirst(offset).prefix(4))
        }
        return (ipv4, "fd" + String(hex.suffix(2)) + ":" + groups.joined(separator: ":") + "::/64")
    }

    func networkInfo(_ name: String) throws -> [String: Any] {
        let data = try run(["network", "inspect", name], capture: true)
        guard let records = try JSONSerialization.jsonObject(with: data) as? [[String: Any]], records.count == 1,
              let info = records.first, info["id"] as? String == name else { throw BoxError("Unexpected network metadata.") }
        return info
    }

    func policy(for record: BoxRecord) throws -> NetworkPolicy {
        let name = "mybox-net-" + String(record.id.dropFirst(namePrefix.count))
        let labels = record.configuration["labels"] as? [String: String] ?? [:]
        guard labels[networkLabel] == name,
              let attachments = record.configuration["networks"] as? [[String: Any]], attachments.count == 1,
              attachments[0]["network"] as? String == name else {
            throw BoxError("This older box uses a shared network. Its data is unchanged. It needs a dedicated network before MyBox can start or enter it.")
        }
        let info = try networkInfo(name)
        guard let config = info["configuration"] as? [String: Any],
              (config["labels"] as? [String: String])?[managedLabel] == "true",
              config["mode"] as? String == "nat",
              let status = info["status"] as? [String: Any],
              let subnet4 = status["ipv4Subnet"] as? String,
              let subnet6 = status["ipv6Subnet"] as? String,
              let gateway = status["ipv4Gateway"] as? String,
              record.ports.allSatisfy({ $0["count"] as? Int == 1 && $0["containerPort"] is Int }) else {
            throw BoxError("The box does not have a supported dedicated network.")
        }
        let saved = try record.portConfiguration()?.mappings ?? []
        let policy = NetworkPolicy(owner: getuid(), network: name, subnet4: subnet4, subnet6: subnet6,
                                   gateway: gateway, ports: Array(Set(record.ports.compactMap { $0["containerPort"] as? Int } + saved.map(\.inside))).sorted())
        try policy.validate()
        return policy
    }

    func checkNetworkMembers(_ policy: NetworkPolicy, allowed: Set<String>) throws {
        for item in try records(["list", "--all", "--format", "json"]) {
            let networks = item.configuration["networks"] as? [[String: Any]] ?? []
            if networks.contains(where: { $0["network"] as? String == policy.network }) && !allowed.contains(item.id) {
                throw BoxError("The box's dedicated network contains another container. The operation was refused.")
            }
        }
    }

    func removeAbandonedHelpers(on network: String) throws {
        for item in try records(["list", "--all", "--format", "json"]) {
            let labels = item.configuration["labels"] as? [String: String] ?? [:]
            let networks = item.configuration["networks"] as? [[String: Any]] ?? []
            guard labels[helperLabel] == "true", matches(item.id, "^mybox-helper-[a-f0-9-]{36}$"),
                  networks.count == 1, networks[0]["network"] as? String == network else { continue }
            if item.state != "stopped" { _ = try run(["stop", item.id], capture: true) }
            _ = try run(["delete", item.id], capture: true)
        }
    }

    func protectedReady(_ name: String, progress: ((String) -> Void)? = nil) throws -> String {
        try installedFirewall()
        let lock = try networkLock()
        defer { withExtendedLifetime(lock) {} }
        let record = try managed(name)
        try recoverPortChange(record)
        let policy = try policy(for: record)
        guard ["stopped", "running"].contains(record.state) else { throw BoxError("The box is \(record.state). Wait for its current operation to finish.") }
        try removeAbandonedHelpers(on: policy.network)
        try checkNetworkMembers(policy, allowed: [record.id])
        if record.state == "running" {
            try firewallRequest("verify", policy: policy)
        } else if usesFork {
            var requestedStart = false
            do {
                let bridge = try withPreparedBridge(policy.network) {
                    let bridge = try policy.bridge(in: hostInterfaces())
                    try firewallRequest("apply", policy: policy)
                    try firewallRequest("verify", policy: policy)
                    guard try policy.bridge(in: hostInterfaces()) == bridge else {
                        throw BoxError("The box network bridge changed before startup.")
                    }
                    progress?("Firewall rules checked. Starting Linux...")
                    requestedStart = true
                    _ = try run(["start", record.id], capture: true)
                    return bridge
                }
                guard try policy.bridge(in: hostInterfaces()) == bridge else {
                    throw BoxError("The box lost its protected bridge after the temporary endpoint closed.")
                }
                try firewallRequest("verify", policy: policy)
            } catch {
                if requestedStart { _ = try? run(["stop", record.id], capture: true) }
                throw error
            }
        } else {
            // Create a fresh helper from the trusted base image on each start.
            // Never reuse a helper filesystem that a project could have changed.
            let helper = "mybox-helper-" + UUID().uuidString.lowercased()
            var helperCreated = false
            var boxStarted = false
            do {
                progress?("Preparing firewall protection...")
                _ = try run(["create", "--name", helper, "--label", "\(helperLabel)=true", "--network", policy.network,
                             "--cpus", "1", "--memory", "256m", "--read-only", "--entrypoint", "/bin/sleep", trustedImage, "infinity"], capture: true)
                helperCreated = true
                _ = try run(["start", helper], capture: true)
                var bridge: String?
                for _ in 0..<50 {
                    bridge = try? policy.bridge(in: hostInterfaces())
                    if bridge != nil { break }
                    Thread.sleep(forTimeInterval: 0.1)
                }
                guard let bridge else { throw BoxError("The trusted helper did not create the expected bridge.") }
                try checkNetworkMembers(policy, allowed: [record.id, helper])
                try firewallRequest("apply", policy: policy)
                try firewallRequest("verify", policy: policy)
                guard try policy.bridge(in: hostInterfaces()) == bridge else { throw BoxError("The network bridge changed before startup.") }
                progress?("Firewall rules checked. Starting Linux...")
                _ = try run(["start", record.id], capture: true)
                boxStarted = true
                guard try policy.bridge(in: hostInterfaces()) == bridge else { throw BoxError("The box did not retain its protected bridge.") }
                progress?("Checking firewall protection and /workspace...")
                try firewallRequest("verify", policy: policy)
                _ = try run(["stop", helper], capture: true)
                _ = try run(["delete", helper], capture: true)
                helperCreated = false
                guard try policy.bridge(in: hostInterfaces()) == bridge else { throw BoxError("The protected bridge disappeared after helper cleanup.") }
            } catch {
                // Keep rules in place if stopping a started guest fails.
                if boxStarted { _ = try? run(["stop", record.id], capture: true) }
                if helperCreated {
                    _ = try? run(["stop", helper], capture: true)
                    _ = try? run(["delete", helper], capture: true)
                }
                throw error
            }
        }
        _ = try run(["exec", record.id, "/bin/sh", "-c", "test -d /workspace && test ! -L /workspace"], capture: true)
        if try record.portConfiguration() != nil { try restoreForwarder(managed(name)) }
        // End the firewall authorization before handing control to a shell or
        // copying data. A later request needs a new operation and approval.
        try finishBoxAccessPreparation()
        return record.id
    }

    func protectedStop(_ name: String) throws {
        let lock = try networkLock()
        defer { withExtendedLifetime(lock) {} }
        try stopManagedBox(managed(name))
    }

    // Callers hold the network lock throughout stopping and any later deletion.
    func stopManagedBox(_ record: BoxRecord) throws {
        let name = String(record.id.dropFirst(namePrefix.count))
        let stop = {
            try self.stopForwarder(record)
            if record.state != "stopped" { _ = try self.run(["stop", record.id], capture: true) }
            guard try self.managed(name).state == "stopped" else {
                throw BoxError("The box has not stopped. Its network protection remains installed.")
            }
            // Legacy boxes can still be stopped without changing their data.
            if (record.configuration["labels"] as? [String: String])?[networkLabel] != nil {
                try self.firewallAfterStop(record)
            }
        }
        if usesFork, record.state == "running", let network = (record.configuration["labels"] as? [String: String])?[networkLabel] {
            // Keep the bridge until its old firewall rules have been removed.
            // The preparation helper then waits for vmnet cleanup to complete.
            try withPreparedBridge(network, startBox: stop)
        } else {
            try stop()
        }
    }

    private func firewallAfterStop(_ record: BoxRecord) throws {
        try firewallRequest("remove", policy: policy(for: record))
    }
}

// A preparation connection and the VM's allocation each keep the network alive.
// Finishing the last connection releases both the idle interface and network.
private func withPreparedBridge<T>(_ network: String, startBox: () throws -> T) throws -> T {
    let process = Process()
    let input = Pipe()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: forkBridgeExecutable)
    #if MYBOX_TESTING
    if let path = ProcessInfo.processInfo.environment["MYBOX_TEST_BRIDGE"] {
        process.executableURL = URL(fileURLWithPath: path)
    }
    #endif
    process.arguments = [network]
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.standardError
    try process.run()
    func finish() throws {
        input.fileHandleForWriting.closeFile()
        // vmnet teardown is asynchronous; wait for the helper's confirmation.
        for _ in 0..<300 where process.isRunning { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            process.waitUntilExit()
            throw BoxError("Timed out releasing the box's prepared network.")
        }
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            throw BoxError("The network helper could not confirm cleanup.")
        }
    }
    var finished = false
    defer { if !finished { try? finish() } }
    let fd = output.fileHandleForReading.fileDescriptor
    let deadline = ProcessInfo.processInfo.systemUptime + 15
    var ready = [UInt8]()
    while ready.count < 6 {
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        guard remaining > 0 else { throw BoxError("Timed out preparing the box network bridge.") }
        var event = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        let result = poll(&event, 1, Int32(remaining * 1000))
        if result == -1 && errno == EINTR { continue }
        guard result > 0, let next = try output.fileHandleForReading.read(upToCount: 6 - ready.count), !next.isEmpty else {
            if process.isRunning { process.terminate() }
            throw BoxError("The fork could not prepare the box network bridge.")
        }
        ready.append(contentsOf: next)
    }
    guard ready == Array("ready\n".utf8) else { throw BoxError("Unexpected reply from the fork's bridge helper.") }
    let value = try startBox()
    finished = true
    try finish()
    return value
}

// Saved subnets must not overlap another box or a currently connected Mac network.
// This selects an address range only; it does not create a vmnet reservation.
func unusedPrivateSubnet(_ occupied: [String]) throws -> String {
    let ranges = try occupied.map { cidr -> ClosedRange<UInt32> in
        let parts = cidr.split(separator: "/")
        guard parts.count == 2, let prefix = Int(parts[1]), (0...32).contains(prefix) else {
            throw BoxError("Cannot read a network subnet: \(cidr)")
        }
        let octets = parts[0].split(separator: ".").compactMap { UInt8($0) }
        guard octets.count == 4 else { throw BoxError("Cannot read a network subnet: \(cidr)") }
        let address = octets.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        let mask: UInt32 = prefix == 0 ? 0 : UInt32.max << (32 - prefix)
        return (address & mask)...(address | ~mask)
    }
    let blocks: [(UInt32, Int)] = [(0x0a000000, 65536), (0xac100000, 4096), (0xc0a80000, 256)]
    for (base, count) in blocks {
        let offset = Int.random(in: 0..<count)
        for index in 0..<count {
            let lower = base + UInt32((offset + index) % count) * 256
            if ranges.allSatisfy({ !$0.overlaps(lower...(lower + 255)) }) {
                return [24, 16, 8, 0].map { String((lower >> $0) & 255) }.joined(separator: ".") + "/24"
            }
        }
    }
    throw BoxError("No unused private /24 subnet is available for this box.")
}
