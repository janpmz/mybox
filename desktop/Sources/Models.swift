import Foundation

struct Workspace: Identifiable, Equatable, Sendable {
    var id: String { name }
    let name: String
    let state: String
    let cores: String
    let memory: String
    let diskUsed: String
    let diskCapacity: String
    let ports: [PortMapping]
    var isRunning: Bool { state == "running" }
    var stateLabel: String { state.capitalized }

    static let examples = [
        Workspace(name: "little-universe", state: "running", cores: "4", memory: "4 GB", diskUsed: "2.8 GB", diskCapacity: "20 GB", ports: [PortMapping(inside: 3000, outside: 8766)]),
        Workspace(name: "weekend-project", state: "running", cores: "2", memory: "2 GB", diskUsed: "1.4 GB", diskCapacity: "20 GB", ports: [PortMapping(inside: 8080, outside: 8080)]),
        Workspace(name: "a-fresh-start", state: "stopped", cores: "2", memory: "2 GB", diskUsed: "864 MB", diskCapacity: "20 GB", ports: [])
    ]
}

struct PortMapping: Identifiable, Equatable, Sendable {
    var id: Int { outside }
    let inside: Int
    let outside: Int
    var url: URL { URL(string: "http://localhost:\(outside)")! }
}

struct DesktopError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Decode numeric values from the CLI; table layout and terminal links are not an API.
enum WorkspaceParser {
    static func parse(_ output: String) throws -> [Workspace] {
        let list: BoxList
        do {
            list = try JSONDecoder().decode(BoxList.self, from: Data(output.utf8))
        } catch {
            throw DesktopError("MyBox returned an invalid JSON workspace list. Update the CLI and run mybox setup-service.")
        }
        guard list.schemaVersion == BoxList.currentSchemaVersion else {
            throw DesktopError("MyBox returned an unsupported workspace list version. Update the desktop app and CLI together.")
        }
        var names = Set<String>()
        return try list.boxes.map { box in
            guard validName(box.name), !box.state.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  names.insert(box.name).inserted else {
                throw DesktopError("MyBox returned an invalid or duplicate workspace name.")
            }
            guard box.cpus.map({ $0 > 0 }) ?? true,
                  box.memoryBytes.map({ $0 > 0 }) ?? true,
                  box.diskCapacityBytes.map({ $0 > 0 }) ?? true else {
                throw DesktopError("MyBox returned invalid workspace resources.")
            }
            guard Set(box.ports.map(\.outside)).count == box.ports.count,
                  box.ports.allSatisfy({ (1...65535).contains($0.inside) && (1...65535).contains($0.outside) }) else {
                throw DesktopError("MyBox returned an invalid or duplicate port mapping.")
            }
            return Workspace(name: box.name, state: box.state, cores: box.cpus.map(String.init) ?? "-",
                             memory: box.memoryBytes.map(memorySize) ?? "-",
                             diskUsed: box.diskUsedBytes.map(diskSize) ?? "-",
                             diskCapacity: box.diskCapacityBytes.map(diskSize) ?? "-",
                             ports: box.ports.map { PortMapping(inside: $0.inside, outside: $0.outside) })
        }
    }

    static func validName(_ value: String) -> Bool {
        guard let range = value.range(of: "^[a-z][a-z0-9-]{0,47}$", options: .regularExpression) else { return false }
        return range == value.startIndex..<value.endIndex
    }
}

enum ShellCommand {
    static func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }
    static func line(executable: String, arguments: [String]) -> String {
        ([executable] + arguments).map(quote).joined(separator: " ")
    }
}
