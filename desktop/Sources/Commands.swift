import Foundation

enum FolderInput {
    static func validate(_ urls: [URL]) throws -> [URL] {
        guard !urls.isEmpty else {
            throw DesktopError("Choose or drop folders from your Mac.")
        }
        var folders: [URL] = [], paths = Set<String>(), names = Set<String>()
        for url in urls {
            guard url.isFileURL else { throw DesktopError("Choose folders from your Mac.") }
            let folder = url.standardizedFileURL
            let values = try folder.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey, .isReadableKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                throw DesktopError("Choose folders, rather than files or links to folders.")
            }
            guard values.isReadable == true else { throw DesktopError("MyBox cannot read \(folder.lastPathComponent). Choose a folder you can open.") }
            let resolved = folder.resolvingSymlinksInPath()
            guard resolved.path != "/", resolved != FileManager.default.homeDirectoryForCurrentUser.resolvingSymlinksInPath() else {
                throw DesktopError("Choose project folders, rather than the entire Mac disk or home folder.")
            }
            guard paths.insert(resolved.path).inserted else { continue }
            guard names.insert(folder.lastPathComponent).inserted else {
                throw DesktopError("Two folders are named “\(folder.lastPathComponent)”. Rename one before adding both to the same box.")
            }
            folders.append(folder)
        }
        return folders
    }

    static func suggestedName(for folder: URL, existing: [String]) -> String {
        var base = folder.lastPathComponent.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
            .lowercased().replacingOccurrences(of: "[^a-z0-9]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        if base.isEmpty { base = "folder" }
        if base.first?.isNumber == true { base = "box-" + base }
        base = String(base.prefix(48)).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        var candidate = base, number = 2
        while existing.contains(candidate) {
            let suffix = "-\(number)"
            candidate = String(base.prefix(48 - suffix.count)).trimmingCharacters(in: CharacterSet(charactersIn: "-")) + suffix
            number += 1
        }
        return candidate
    }
}

struct PortDraft: Identifiable, Equatable {
    let id = UUID()
    var outside = ""
    var inside = ""
    func mapping() throws -> PortMapping {
        func port(_ input: String) throws -> Int {
            guard input.range(of: "^[0-9]+$", options: .regularExpression) != nil,
                  let value = Int(input), (1...65535).contains(value) else {
                throw DesktopError("Ports must be between 1 and 65535.")
            }
            return value
        }
        return try PortMapping(inside: port(inside), outside: port(outside))
    }
}

struct PortSelection {
    private(set) var confirmed: [PortDraft] = []
    var draft = PortDraft()
    var hasDraft: Bool { !draft.outside.isEmpty || !draft.inside.isEmpty }
    var validation: String? {
        guard confirmed.count < 64 else { return "A box supports up to 64 port mappings." }
        do {
            let mapping = try draft.mapping()
            guard !confirmed.contains(where: { (try? $0.mapping().outside) == mapping.outside }) else {
                return "That outside port is already mapped."
            }
            return nil
        } catch { return error.localizedDescription }
    }
    mutating func add() throws {
        if let validation { throw DesktopError(validation) }
        confirmed.append(draft)
        draft = PortDraft()
    }
    mutating func remove(_ id: UUID) { confirmed.removeAll { $0.id == id } }
}

enum BoxCommand {
    static func newName(_ name: String, existing: [String]) throws {
        guard WorkspaceParser.validName(name), !name.hasSuffix("-") else {
            throw DesktopError("Use 1–48 lowercase letters, numbers, or hyphens. Start with a letter; end with a letter or number.")
        }
        guard !existing.contains(name) else { throw DesktopError("That name is already in use.") }
    }
    static func create(name: String, cpus: String, memory: String, ports: [PortDraft], existing: [String]) throws -> [String] {
        try newName(name, existing: existing)
        guard positiveInteger(cpus) else { throw DesktopError("CPU must be a positive whole number.") }
        let memoryArgument = try SizeInput.memoryArgument(memory)
        guard ports.count <= 64 else { throw DesktopError("A box supports up to 64 port mappings.") }
        var result = ["create", name, "--cpus", cpus, "--memory", memoryArgument]
        var used = Set<Int>()
        for draft in ports {
            let mapping = try draft.mapping()
            guard used.insert(mapping.outside).inserted else { throw DesktopError("Each outside port can appear only once.") }
            result += ["--inside-port", String(mapping.inside), "--outside-port", String(mapping.outside)]
        }
        return result
    }
    static func copy(source: String, name: String, existing: [String]) throws -> [String] {
        try newName(name, existing: existing + [source])
        return ["copy", source, name]
    }
    static func resize(box: Workspace, cpus: String, memory: String, disk: String) throws -> [String] {
        guard box.state == "stopped" else { throw DesktopError("Stop the box before resizing.") }
        var result = ["resize", box.name]
        if !cpus.isEmpty {
            guard positiveInteger(cpus) else { throw DesktopError("CPU must be a positive whole number.") }
            result += ["--cpus", cpus]
        }
        for (value, flag) in [(memory, "--memory-gb"), (disk, "--disk-size-gb")] where !value.isEmpty {
            let normalized = value.replacingOccurrences(of: ",", with: ".")
            guard normalized.range(of: "^[0-9]+([.][0-9]+)?$", options: .regularExpression) != nil,
                  let number = Double(normalized), number.isFinite, number > 0 else {
                throw DesktopError("Enter a positive size in GB.")
            }
            result += [flag, normalized]
        }
        guard result.count > 2 else { throw DesktopError("Enter a value to change.") }
        return result
    }
    static func command(box: String, text: String) -> [String]? {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !text.contains("\0") else { return nil }
        // Shell syntax is interpreted only inside Linux; host arguments remain quoted.
        return ["enter", box, "--", "/bin/bash", "-lc", text]
    }
    static func validExportPath(_ path: String) -> Bool {
        !path.isEmpty && !path.contains("\0") && !path.components(separatedBy: "/").contains("..") &&
            (!path.hasPrefix("/") || path == "/workspace" || path.hasPrefix("/workspace/"))
    }
    private static func positiveInteger(_ value: String) -> Bool {
        value.range(of: "^[1-9][0-9]*$", options: .regularExpression) != nil && Int(value) != nil
    }
}

enum SizeInput {
    static func memoryArgument(_ input: String) throws -> String {
        let normalized = input.replacingOccurrences(of: ",", with: ".")
        guard normalized.count <= 32,
              normalized.range(of: "^[0-9]+([.][0-9]+)?$", options: .regularExpression) != nil,
              let gb = Decimal(string: normalized, locale: Locale(identifier: "en_US_POSIX")), gb > 0 else {
            throw DesktopError("Enter a positive memory size in GB, such as 2 or 0.5.")
        }
        // Match CLI resize: memory uses binary GB, rounded up to a whole MiB.
        var mib = gb * 1024
        var rounded = Decimal()
        NSDecimalRound(&rounded, &mib, 0, .up)
        guard rounded <= Decimal(Int64.max >> 20) else { throw DesktopError("That memory size is too large.") }
        return "\(NSDecimalNumber(decimal: rounded).uint64Value)m"
    }

    static func gigabytesPlaceholder(_ display: String) -> String {
        let parts = display.split(whereSeparator: \.isWhitespace)
        let scales: [String: Decimal] = ["TB": 1000, "GB": 1, "MB": Decimal(1) / 1000,
                                        "kB": Decimal(1) / 1_000_000, "B": Decimal(1) / 1_000_000_000]
        guard parts.count == 2, let scale = scales[String(parts[1])],
              let value = Decimal(string: parts[0].replacingOccurrences(of: ",", with: "."),
                                  locale: Locale(identifier: "en_US_POSIX")) else { return "" }
        return NSDecimalNumber(decimal: value * scale).stringValue
    }
}

struct PortStatus: Identifiable, Sendable {
    var id: Int { mapping.outside }
    let mapping: PortMapping
    let owner: String
    let state: String
    let fixed: Bool
}

struct PortList: Sendable {
    let ports: [PortStatus]
    let warning: String?
    func forwardingWarning(isRunning: Bool) -> String? {
        isRunning && ports.contains(where: { $0.owner == "MyBox" }) ? warning : nil
    }
    static func parse(_ output: String) throws -> PortList {
        let lines = output.components(separatedBy: .newlines)
        guard let header = lines.firstIndex(where: { $0.hasPrefix("INSIDE  OUTSIDE") }) else {
            throw DesktopError("MyBox returned an unrecognized port list.")
        }
        var ports: [PortStatus] = [], warning: String?
        for line in lines.dropFirst(header + 1) where !line.isEmpty {
            if line.hasPrefix("Outside ports bind") || line == "No port mappings." { continue }
            if line.hasPrefix("Forwarding stopped:") { warning = line; continue }
            let fields = line.split(whereSeparator: \.isWhitespace).map(String.init)
            guard (4...5).contains(fields.count), ["Apple", "MyBox"].contains(fields[2]),
                  ["active", "saved"].contains(fields[3]), fields.count == 4 || fields[4] == "(fixed)" else {
                throw DesktopError("MyBox returned an unrecognized port row.")
            }
            let mapping = try PortDraft(outside: fields[1], inside: fields[0]).mapping()
            guard !ports.contains(where: { $0.id == mapping.outside }) else { throw DesktopError("Duplicate outside port in the port list.") }
            ports.append(PortStatus(mapping: mapping, owner: fields[2], state: fields[3], fixed: fields[2] == "Apple" || fields.count == 5))
        }
        return PortList(ports: ports, warning: warning)
    }
}
