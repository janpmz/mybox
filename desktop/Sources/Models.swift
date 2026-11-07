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

/// Reads the installed CLI's table, so launching the app needs no privileged
/// service upgrade. Header offsets preserve spaces in sizes and port mappings.
enum WorkspaceParser {
    static func parse(_ output: String) throws -> [Workspace] {
        let lines = output.components(separatedBy: .newlines)
        if lines.contains(where: { $0.hasPrefix("No MyBox boxes yet.") }) { return [] }
        let titles = ["NAME", "STATE", "CORES", "MEMORY", "DISK USED", "DISK MAX", "PORTS"]
        guard let headerIndex = lines.firstIndex(where: { $0.hasPrefix("NAME ") }) else {
            throw DesktopError("MyBox returned an unrecognized workspace list. Check that the CLI is up to date.")
        }
        let header = lines[headerIndex] as NSString
        let offsets = titles.map { header.range(of: $0).location }
        guard !offsets.contains(NSNotFound), offsets == offsets.sorted() else {
            throw DesktopError("The MyBox list is missing expected columns.")
        }
        var result: [Workspace] = []
        for line in lines.dropFirst(headerIndex + 1) {
            if line.trimmingCharacters(in: .whitespaces).isEmpty { break }
            let row = line as NSString
            guard row.length >= offsets.last! else { throw DesktopError("MyBox returned an incomplete workspace row.") }
            let cells = offsets.enumerated().map { index, start -> String in
                let end = index + 1 < offsets.count ? offsets[index + 1] : row.length
                return row.substring(with: NSRange(location: start, length: end - start)).trimmingCharacters(in: .whitespaces)
            }
            guard validName(cells[0]), !cells[1].isEmpty, !result.contains(where: { $0.name == cells[0] }) else {
                throw DesktopError("MyBox returned an invalid or duplicate workspace name.")
            }
            result.append(Workspace(name: cells[0], state: cells[1], cores: cells[2], memory: cells[3],
                                    diskUsed: cells[4], diskCapacity: cells[5], ports: try parsePorts(cells[6])))
        }
        return result
    }

    static func validName(_ value: String) -> Bool {
        value.range(of: "^[a-z][a-z0-9-]{0,47}$", options: .regularExpression) != nil
    }

    static func parsePorts(_ value: String) throws -> [PortMapping] {
        if value == "-" || value.isEmpty { return [] }
        let expression = try NSRegularExpression(pattern: "^inside ([0-9]+) / outside (?:http://localhost:)?([0-9]+)(?: \\(localhost\\))?$")
        return try value.components(separatedBy: ", ").map { mapping in
            let ns = mapping as NSString
            guard let match = expression.firstMatch(in: mapping, range: NSRange(location: 0, length: ns.length)),
                  let inside = Int(ns.substring(with: match.range(at: 1))),
                  let outside = Int(ns.substring(with: match.range(at: 2))),
                  (1...65535).contains(inside), (1...65535).contains(outside) else {
                throw DesktopError("MyBox returned an unrecognized port mapping: \(mapping)")
            }
            return PortMapping(inside: inside, outside: outside)
        }
    }
}

enum ShellCommand {
    static func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }
    static func line(executable: String, arguments: [String]) -> String {
        ([executable] + arguments).map(quote).joined(separator: " ")
    }
}
