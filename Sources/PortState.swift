import Foundation
import Darwin

struct PortMapping: Codable, Equatable {
    let inside: Int
    let outside: Int
}

struct PortTarget: Codable, Equatable {
    let address: String
    let started: String
}

struct PortConfiguration: Codable {
    var version = 1
    let id: String
    let created: String
    var mappings: [PortMapping]
    var target: PortTarget?
    var revision = UUID().uuidString

    func validate() throws {
        guard version == 1, matches(id, "^mybox-[a-z][a-z0-9-]{0,47}$"), !created.isEmpty,
              UUID(uuidString: revision) != nil, mappings.count <= 64,
              Set(mappings.map(\.outside)).count == mappings.count,
              mappings.allSatisfy({ (1...65535).contains($0.inside) && (1...65535).contains($0.outside) }) else {
            throw BoxError("Invalid saved port configuration.")
        }
        if let target {
            var address = in_addr()
            guard inet_pton(AF_INET, target.address, &address) == 1, !target.started.isEmpty else {
                throw BoxError("Invalid forwarding destination.")
            }
        }
    }
}

let portForwarderProtocolVersion = 2

struct PortStatus: Codable {
    var protocolVersion: Int? = portForwarderProtocolVersion
    let revision: String
    let pid: Int32
    let active: Bool
    let error: String?
}

func portRoot() -> URL {
    #if MYBOX_TESTING
    if let path = ProcessInfo.processInfo.environment["MYBOX_TEST_PORT_ROOT"] { return URL(fileURLWithPath: path) }
    #endif
    return files.homeDirectoryForCurrentUser.appendingPathComponent(".config/mybox/ports", isDirectory: true)
}

func portDirectory(_ id: String) -> URL { portRoot().appendingPathComponent(id, isDirectory: true) }

func securePortDirectory(_ directory: URL) throws {
    try files.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    var info = stat()
    guard lstat(directory.path, &info) == 0, info.st_uid == getuid(),
          info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR), info.st_mode & 0o077 == 0 else {
        throw BoxError("The port directory must be private and belong to the current Mac user.")
    }
}

func readPortJSON<T: Decodable>(_ type: T.Type, _ url: URL) throws -> T {
    let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw BoxError("Cannot read \(url.lastPathComponent): \(String(cString: strerror(errno)))") }
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    var info = stat()
    guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
          info.st_size <= 65536, info.st_mode & 0o022 == 0 else { throw BoxError("Unsafe port configuration file.") }
    return try JSONDecoder().decode(type, from: handle.readDataToEndOfFile())
}

func writePortJSON<T: Encodable>(_ value: T, _ url: URL) throws {
    try JSONEncoder().encode(value).write(to: url, options: .atomic)
    guard chmod(url.path, 0o600) == 0 else { throw BoxError("Cannot secure the port configuration file.") }
}

func readPortConfiguration(_ directory: URL) throws -> PortConfiguration {
    let config = try readPortJSON(PortConfiguration.self, directory.appendingPathComponent("ports.json"))
    try config.validate()
    return config
}

func savePortConfiguration(_ config: PortConfiguration) throws {
    try config.validate()
    let directory = portDirectory(config.id)
    try securePortDirectory(portRoot())
    try securePortDirectory(directory)
    try writePortJSON(config, directory.appendingPathComponent("ports.json"))
}
