import Foundation
import Darwin

private func outsidePortLink(_ port: Int) -> String {
    let url = "http://localhost:\(port)"
    guard isatty(STDOUT_FILENO) == 1 else { return url }
    // OSC 8 sets the hyperlink target. Keep the URL visible for terminals
    // that only support opening URLs detected in the displayed text.
    return "\u{1B}]8;;\(url)\u{1B}\\\u{1B}[4m\(url)\u{1B}[24m\u{1B}]8;;\u{1B}\\"
}

struct PortOptions {
    let action: String
    let name: String
    let mapping: PortMapping?
    let outside: Int?

    init(_ args: [String]) throws {
        guard args.count >= 2, ["add", "remove", "list"].contains(args[0]) else {
            throw BoxError("Usage: mybox ports <add|remove|list> <name> [--inside-port N] [--outside-port N]")
        }
        action = args[0]
        name = args[1]
        _ = try containerID(name)
        var values: [String: Int] = [:]
        var index = 2
        while index < args.count {
            let flag = args[index]
            guard action != "list", ["--inside-port", "--outside-port"].contains(flag),
                  !(action == "remove" && flag == "--inside-port"), values[flag] == nil, index + 1 < args.count,
                  matches(args[index + 1], "^[0-9]+$"), let value = Int(args[index + 1]), (1...65535).contains(value) else {
                throw BoxError("Use each required port flag once, with a number from 1 to 65535.")
            }
            values[flag] = value
            index += 2
        }
        outside = values["--outside-port"]
        if action == "add" {
            guard let inside = values["--inside-port"], let outside else {
                throw BoxError("Usage: mybox ports add <name> --inside-port N --outside-port N")
            }
            mapping = PortMapping(inside: inside, outside: outside)
        } else {
            guard action != "remove" || outside != nil else {
                throw BoxError("Usage: mybox ports remove <name> --outside-port N")
            }
            mapping = nil
        }
    }
}

extension BoxRecord {
    func portConfiguration() throws -> PortConfiguration? {
        let directory = portDirectory(id)
        let path = directory.appendingPathComponent("ports.json")
        guard exists(path) else { return nil }
        let config = try readPortConfiguration(directory)
        // Do not apply settings to a different container that reuses a name.
        guard config.id == id, config.created == configuration["creationDate"] as? String else { return nil }
        return config
    }

    func freshPortConfiguration(_ mappings: [PortMapping]) throws -> PortConfiguration {
        guard let created = configuration["creationDate"] as? String, !created.isEmpty else {
            throw BoxError("Cannot identify this box's creation time.")
        }
        return PortConfiguration(id: id, created: created, mappings: mappings, target: nil)
    }

    func forwardingTarget() throws -> PortTarget {
        guard state == "running", let started = status["startedDate"] as? String,
              let networks = status["networks"] as? [[String: Any]], networks.count == 1,
              networks[0]["network"] as? String == "mybox-net-" + String(id.dropFirst(namePrefix.count)),
              let cidr = networks[0]["ipv4Address"] as? String, let address = cidr.split(separator: "/").first else {
            throw BoxError("Cannot identify the running box's network address.")
        }
        return PortTarget(address: String(address), started: started)
    }

    func allPortMappings() throws -> [BoxListEntry.Port] {
        let dynamic = try portConfiguration()?.mappings ?? []
        return ports.map { BoxListEntry.Port(inside: $0["containerPort"] as? Int ?? 0, outside: $0["hostPort"] as? Int ?? 0) }
            + dynamic.map { BoxListEntry.Port(inside: $0.inside, outside: $0.outside) }
    }

    func allPortDescription() throws -> String {
        let descriptions = try allPortMappings().map { "inside \($0.inside) / outside \(outsidePortLink($0.outside))" }
        return descriptions.isEmpty ? "-" : descriptions.joined(separator: ", ")
    }
}

func forwarderIsRunning(_ directory: URL) throws -> Bool {
    let path = directory.appendingPathComponent("runner.lock")
    guard exists(path) else { return false }
    let fd = open(path.path, O_RDWR | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw BoxError("Cannot inspect the forwarding lock.") }
    defer { close(fd) }
    if flock(fd, LOCK_EX | LOCK_NB) == 0 { _ = flock(fd, LOCK_UN); return false }
    guard errno == EWOULDBLOCK else { throw BoxError("Cannot inspect the forwarding lock.") }
    return true
}

func synchronizeForwarder(_ config: PortConfiguration, backend: Backend) throws {
    let directory = portDirectory(config.id)
    #if MYBOX_TESTING
    if ProcessInfo.processInfo.environment["MYBOX_TEST_FAKE_FORWARDER"] == "1" {
        if ProcessInfo.processInfo.environment["MYBOX_TEST_FORWARDER_FAIL"] == "1", !config.mappings.isEmpty {
            throw BoxError("Test forwarding failure.")
        }
        try writePortJSON(PortStatus(revision: config.revision, pid: getpid(), active: config.target != nil && !config.mappings.isEmpty, error: nil),
                          directory.appendingPathComponent("status.json"))
        return
    }
    #endif
    let active = config.target != nil && !config.mappings.isEmpty
    if try !active && !forwarderIsRunning(directory) { return }
    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        .deletingLastPathComponent().appendingPathComponent("mybox-port-forwarder")
    let previousStatus = try? readPortJSON(PortStatus.self, directory.appendingPathComponent("status.json"))
    if try active && forwarderIsRunning(directory) && previousStatus?.protocolVersion != portForwarderProtocolVersion {
        // Older helpers understand a nil target. Stop through their saved config
        // instead of sending a signal to a possibly stale PID.
        note("Updating port forwarding support...")
        var stopping = config
        stopping.target = nil
        stopping.revision = UUID().uuidString
        try savePortConfiguration(stopping)
        do {
            let deadline = Date().addingTimeInterval(5)
            while try forwarderIsRunning(directory) && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            guard try !forwarderIsRunning(directory) else { throw BoxError("The previous port helper did not stop. Retry the command.") }
        } catch {
            try savePortConfiguration(config)
            throw error
        }
        try savePortConfiguration(config)
    }
    let deadline = Date().addingTimeInterval(10)
    var lastSpawn = Date.distantPast
    while Date() < deadline {
        let status = try? readPortJSON(PortStatus.self, directory.appendingPathComponent("status.json"))
        if status?.revision == config.revision {
            if let error = status?.error { throw BoxError(error) }
            if try status?.active == active && (!active || forwarderIsRunning(directory)) { return }
        }
        if try active && Date().timeIntervalSince(lastSpawn) > 0.5 && !forwarderIsRunning(directory) {
            guard files.isExecutableFile(atPath: executable.path) else { throw BoxError("The port helper is missing. Run make build.") }
            let process = Process()
            process.executableURL = executable
            process.arguments = [directory.path, backend.executable]
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            let log = directory.appendingPathComponent("helper.log")
            let fd = open(log.path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard fd >= 0 else { throw BoxError("Cannot open the port helper log.") }
            let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            process.standardError = handle
            try process.run()
            lastSpawn = Date()
        }
        if try !active && !forwarderIsRunning(directory) { return }
        Thread.sleep(forTimeInterval: 0.05)
    }
    throw BoxError("The port helper did not confirm the change. Read \(directory.path)/helper.log.")
}

extension Backend {
    func restoreForwarder(_ record: BoxRecord) throws {
        guard var config = try record.portConfiguration() else { return }
        config.target = record.state == "running" && !config.mappings.isEmpty ? try record.forwardingTarget() : nil
        config.revision = UUID().uuidString
        try savePortConfiguration(config)
        try synchronizeForwarder(config, backend: self)
    }

    func stopForwarder(_ record: BoxRecord) throws {
        guard var config = try record.portConfiguration() else { return }
        config.target = nil
        config.revision = UUID().uuidString
        try savePortConfiguration(config)
        try synchronizeForwarder(config, backend: self)
    }

    // A journal restores the previous settings if a command exits between the
    // helper change and the firewall change. Callers hold the network lock.
    func recoverPortChange(_ record: BoxRecord) throws {
        let pending = portDirectory(record.id).appendingPathComponent("pending.json")
        guard exists(pending) else { return }
        var previous = try readPortJSON(PortConfiguration.self, pending)
        try previous.validate()
        guard previous.id == record.id, previous.created == record.configuration["creationDate"] as? String else {
            throw BoxError("An interrupted port change belongs to a different box. Its saved files need review.")
        }
        previous.target = record.state == "running" && !previous.mappings.isEmpty ? try record.forwardingTarget() : nil
        previous.revision = UUID().uuidString
        try savePortConfiguration(previous)
        try synchronizeForwarder(previous, backend: self)
        if record.state == "running" { try firewallRequest("update-ports", policy: policy(for: record)) }
        try files.removeItem(at: pending)
    }

    func changePorts(_ options: PortOptions) throws {
        let lock = try networkLock()
        defer { withExtendedLifetime(lock) {} }
        let record = try managed(options.name)
        guard ["running", "stopped"].contains(record.state) else { throw BoxError("Wait for the current box operation to finish.") }
        if options.action == "list" {
            let config = try record.portConfiguration()
            let status = try? readPortJSON(PortStatus.self, portDirectory(record.id).appendingPathComponent("status.json"))
            print("INSIDE  OUTSIDE  OWNER   STATE")
            for port in record.ports {
                print("\(port["containerPort"] as? Int ?? 0)  \(port["hostPort"] as? Int ?? 0)  Apple   \(record.state == "running" ? "active" : "saved") (fixed)")
            }
            for port in config?.mappings ?? [] {
                let active = try record.state == "running" && status?.revision == config?.revision && status?.active == true
                    && forwarderIsRunning(portDirectory(record.id))
                print("\(port.inside)  \(port.outside)  MyBox   \(active ? "active" : "saved")")
            }
            if record.ports.isEmpty && (config?.mappings.isEmpty ?? true) { print("No port mappings.") }
            if record.state == "running", !(config?.mappings.isEmpty ?? true),
               status?.revision == config?.revision, let error = status?.error {
                print("Forwarding stopped: \(error)")
            }
            print("Outside ports bind to 127.0.0.1 on the Mac. Mappings use TCP.")
            return
        }
        try installedFirewall()
        try recoverPortChange(record)
        let old = try record.portConfiguration() ?? record.freshPortConfiguration([])
        var next = old
        if options.action == "add", let mapping = options.mapping {
            guard !record.ports.contains(where: { $0["hostPort"] as? Int == mapping.outside }),
                  !next.mappings.contains(where: { $0.outside == mapping.outside }) else {
                throw BoxError("Outside port \(mapping.outside) already has a mapping. Remove it before adding a different mapping.")
            }
            next.mappings.append(mapping)
        } else if let outside = options.outside {
            guard !record.ports.contains(where: { $0["hostPort"] as? Int == outside }) else {
                throw BoxError("Apple manages this original mapping. Apple container 1.6.1 cannot remove it from an existing container.")
            }
            guard next.mappings.contains(where: { $0.outside == outside }) else { throw BoxError("Outside port \(outside) has no MyBox mapping.") }
            next.mappings.removeAll { $0.outside == outside }
        }
        next.mappings.sort { $0.outside < $1.outside }
        next.revision = UUID().uuidString
        next.target = record.state == "running" && !next.mappings.isEmpty ? try record.forwardingTarget() : nil
        try next.validate()
        let oldPolicy = try policy(for: record)
        var nextPolicy = oldPolicy
        nextPolicy.ports = Array(Set(record.ports.compactMap { $0["containerPort"] as? Int } + next.mappings.map(\.inside))).sorted()
        try nextPolicy.validate()
        try checkNetworkMembers(oldPolicy, allowed: [record.id])
        if record.state == "running" { try firewallRequest("verify", policy: oldPolicy) }
        try securePortDirectory(portRoot())
        try securePortDirectory(portDirectory(record.id))
        let pending = portDirectory(record.id).appendingPathComponent("pending.json")
        try writePortJSON(old, pending)
        do {
            try savePortConfiguration(next)
            try synchronizeForwarder(next, backend: self)
            if record.state == "running" { try firewallRequest("update-ports", policy: nextPolicy) }
            try files.removeItem(at: pending)
        } catch {
            let original = error
            do { try recoverPortChange(record) }
            catch { throw BoxError("Port change failed: \(original). Recovery also failed: \(error). Retry the port command to recover.") }
            throw original
        }
        print("Port mapping \(options.action == "add" ? "added" : "removed") for \(options.name).")
        if record.state == "stopped" { print("The box remains stopped. Saved mappings take effect at its next start.") }
    }
}
