import Foundation
import Darwin

let managedLabel = "com.mybox.managed"
let namePrefix = "mybox-"

func containerID(_ name: String) throws -> String {
    guard matches(name, "^[a-z][a-z0-9-]{0,47}$") else {
        throw BoxError("Use a name of 1–48 lowercase letters, digits, or hyphens, starting with a letter.")
    }
    return namePrefix + name
}

func hostURL(_ path: String) -> URL {
    URL(fileURLWithPath: (path as NSString).expandingTildeInPath).standardizedFileURL
}

enum ItemKind { case file, directory, link, other }

func itemKind(_ url: URL) throws -> ItemKind {
    var metadata = stat()
    guard lstat(url.path, &metadata) == 0 else {
        throw BoxError("Cannot read \(url.path.debugDescription): \(String(cString: strerror(errno)))")
    }
    switch metadata.st_mode & mode_t(S_IFMT) {
    case mode_t(S_IFREG): return .file
    case mode_t(S_IFDIR): return .directory
    case mode_t(S_IFLNK): return .link
    default: return .other
    }
}

func exists(_ url: URL) -> Bool {
    var metadata = stat()
    return lstat(url.path, &metadata) == 0
}

final class TemporaryFolder {
    let url: URL
    init() throws {
        url = files.temporaryDirectory.appendingPathComponent("mybox-" + UUID().uuidString, isDirectory: true)
        try files.createDirectory(at: url, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    }
    deinit { try? files.removeItem(at: url) }
}

struct Backend {
    let executable: String

    init() throws {
        let directories = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let candidates = directories.filter { $0.hasPrefix("/") }.map { $0 + "/container" }
            + ["/usr/local/bin/container", "/opt/homebrew/bin/container"]
        guard let path = candidates.first(where: { files.isExecutableFile(atPath: $0) }) else {
            throw BoxError("Apple's container CLI was not found. It must be installed before using MyBox.")
        }
        executable = path
    }

    @discardableResult
    func run(_ arguments: [String], capture: Bool = false) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardInput = FileHandle.standardInput
        process.standardError = FileHandle.standardError
        let pipe = capture ? Pipe() : nil
        process.standardOutput = pipe ?? (FileHandle.standardOutput as Any)
        try process.run()
        let data = pipe?.fileHandleForReading.readDataToEndOfFile() ?? Data()
        process.waitUntilExit()
        guard process.terminationReason == .exit && process.terminationStatus == 0 else {
            if !data.isEmpty { FileHandle.standardError.write(data) }
            let status = process.terminationReason == .uncaughtSignal ? 128 + process.terminationStatus : process.terminationStatus
            throw CommandFailed(status: status)
        }
        return data
    }

    func replaceProcess(_ arguments: [String]) throws -> Never {
        let pointers = ([executable] + arguments).map { strdup($0) } + [nil]
        defer { pointers.forEach { free($0) } }
        pointers.withUnsafeBufferPointer { buffer in
            _ = execv(executable, buffer.baseAddress!)
        }
        throw BoxError("Cannot open the Linux shell: \(String(cString: strerror(errno)))")
    }

    func records(_ arguments: [String]) throws -> [BoxRecord] {
        let data = try run(arguments, capture: true)
        guard let objects = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
            throw BoxError("Unexpected response from Apple's container CLI.")
        }
        return try objects.map(BoxRecord.init)
    }

    func managed(_ name: String) throws -> BoxRecord {
        let id = try containerID(name)
        let records = try records(["inspect", id])
        guard let record = records.first, records.count == 1, record.id == id, record.isManaged else {
            throw BoxError("\(name.debugDescription) is not a box created by MyBox.")
        }
        try record.checkConfiguration()
        return record
    }

    func ready(_ name: String) throws -> String {
        try protectedReady(name)
    }
}

struct BoxRecord {
    let id: String
    let configuration: [String: Any]
    let state: String

    init(_ object: [String: Any]) throws {
        guard let id = object["id"] as? String,
              let configuration = object["configuration"] as? [String: Any],
              let status = object["status"] as? [String: Any],
              let state = status["state"] as? String else {
            throw BoxError("Unexpected container metadata.")
        }
        self.id = id
        self.configuration = configuration
        self.state = state
    }

    var isManaged: Bool {
        let labels = configuration["labels"] as? [String: String] ?? [:]
        return id.hasPrefix(namePrefix) && labels[managedLabel] == "true"
            && matches(String(id.dropFirst(namePrefix.count)), "^[a-z][a-z0-9-]{0,47}$")
    }

    var ports: [[String: Any]] { configuration["publishedPorts"] as? [[String: Any]] ?? [] }

    func checkConfiguration() throws {
        guard let mounts = configuration["mounts"] as? [Any], mounts.isEmpty,
              let sockets = configuration["publishedSockets"] as? [Any], sockets.isEmpty,
              configuration["ssh"] as? Bool == false,
              configuration["publishedPorts"] is [[String: Any]],
              ports.allSatisfy({ $0["hostAddress"] as? String == "127.0.0.1" && $0["proto"] as? String == "tcp" }) else {
            throw BoxError("Box \(id) has mounts, forwarded sockets, SSH forwarding, or non-localhost ports. This prototype cannot manage that configuration.")
        }
    }

    var portDescription: String {
        ports.map { port in
            let host = port["hostPort"] as? Int ?? 0
            let guest = port["containerPort"] as? Int ?? 0
            return "inside \(guest) / outside \(host) (localhost)"
        }.joined(separator: ", ")
    }
}

struct CreateOptions {
    let name: String
    var ports: [String] = []
    var cpus = "2"
    var memory = "2g"

    init(_ arguments: [String]) throws {
        guard let name = arguments.first else { throw BoxError("Usage: mybox create <name> [--inside-port N --outside-port N] [--cpus N] [--memory 2g]") }
        _ = try containerID(name)
        guard !name.hasSuffix("-") else {
            throw BoxError("A new box name must end with a letter or digit so it can be used as a hostname.")
        }
        self.name = name
        var index = 1
        var insidePorts: [Int] = []
        var outsidePorts: [Int] = []
        while index < arguments.count {
            let option = arguments[index]
            guard index + 1 < arguments.count else { throw BoxError("Missing value for \(option).") }
            let value = arguments[index + 1]
            switch option {
            case "--inside-port", "--outside-port":
                guard matches(value, "^[0-9]+$"), let port = Int(value), (1...65535).contains(port) else {
                    throw BoxError("\(option) must be a port number from 1 to 65535. Outside ports always bind to 127.0.0.1 on the Mac.")
                }
                if option == "--inside-port" {
                    insidePorts.append(port)
                } else {
                    guard !outsidePorts.contains(port) else { throw BoxError("Outside port \(port) was specified twice.") }
                    outsidePorts.append(port)
                }
            case "--port":
                throw BoxError("Use --inside-port and --outside-port instead. Example: --inside-port 8080 --outside-port 8766")
            case "--cpus":
                guard matches(value, "^[1-9][0-9]*$"), Int(value) != nil else { throw BoxError("--cpus must be a positive whole number.") }
                cpus = value
            case "--memory":
                guard matches(value, "^[1-9][0-9]*[mMgG]$") else { throw BoxError("Use --memory with a size such as 512m or 2g.") }
                memory = value.lowercased()
            default: throw BoxError("Unknown create option: \(option).")
            }
            index += 2
        }
        guard insidePorts.count == outsidePorts.count else {
            throw BoxError("Provide both --inside-port and --outside-port for each port mapping.")
        }
        ports = zip(insidePorts, outsidePorts).map { inside, outside in "127.0.0.1:\(outside):\(inside)" }
    }
}

func create(_ options: CreateOptions, backend: Backend) throws {
    try installedFirewall()
    let id = try containerID(options.name)
    let network = "mybox-net-" + options.name
    // A qualified alias avoids changing Apple's global DNS domain or the
    // existing container IDs. Apple persists it and updates its IP on start.
    var arguments = ["create", "--name", id, "--label", "\(managedLabel)=true", "--label", "\(networkLabel)=\(network)",
                     "--cpus", options.cpus, "--memory", options.memory,
                     "--dns", "1.1.1.1", "--dns", "1.0.0.1", "--network", "\(network),alias=\(options.name).box.test"]
    for port in options.ports { arguments += ["--publish", port] }
    arguments += ["--entrypoint", "/bin/sh", "docker.io/library/debian:trixie-slim", "-c", "mkdir -p /workspace && exec sleep infinity"]
    _ = try backend.run(["network", "create", "--label", "\(managedLabel)=true", network], capture: true)
    do { _ = try backend.run(arguments, capture: true) }
    catch {
        _ = try? backend.run(["network", "delete", network], capture: true)
        throw error
    }
    do {
        _ = try backend.ready(options.name)
    } catch {
        note("The box was created. After resolving the error, retry with: mybox start \(options.name)")
        throw error
    }
    print("Created \(options.name). Project files live in /workspace.")
    if !options.ports.isEmpty { print("\nPort forwarding:") }
    for port in options.ports {
        let parts = port.split(separator: ":")
        print("  Inside port:  \(parts[2]) (Linux box)")
        print("  Outside port: \(parts[1]) (localhost only)")
        print("")
    }
    print("Enter with: mybox enter \(options.name)")
}

func stageImport(_ source: URL, to target: URL, skipped: inout Int) throws {
    switch try itemKind(source) {
    case .directory:
        try files.createDirectory(at: target, withIntermediateDirectories: false)
        for child in try files.contentsOfDirectory(at: source, includingPropertiesForKeys: nil).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            try stageImport(child, to: target.appendingPathComponent(child.lastPathComponent), skipped: &skipped)
        }
    case .file:
        try files.copyItem(at: source, to: target)
        let attributes = try files.attributesOfItem(atPath: source.path)
        let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0o644
        try files.setAttributes([.posixPermissions: permissions & 0o777], ofItemAtPath: target.path)
    case .link, .other:
        skipped += 1
    }
}

func validateHostTree(_ root: URL) throws {
    switch try itemKind(root) {
    case .directory:
        for child in try files.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) {
            try validateHostTree(child)
        }
    case .file: break
    case .link, .other: throw BoxError("Transfer contains a symlink or special file: \(root.lastPathComponent.debugDescription).")
    }
}

func importFiles(_ name: String, path: String, backend: Backend) throws {
    let source = hostURL(path)
    let kind = try itemKind(source)
    guard kind == .file || kind == .directory else {
        throw BoxError("Choose a regular file or folder. Import does not support symlinks or special files.")
    }
    guard source.path != "/", source.resolvingSymlinksInPath() != files.homeDirectoryForCurrentUser.resolvingSymlinksInPath() else {
        throw BoxError("Choose a project folder, rather than the entire Mac disk or home directory.")
    }
    let temporary = try TemporaryFolder()
    defer { withExtendedLifetime(temporary) {} }
    let payload = temporary.url.appendingPathComponent("payload")
    var skipped = 0
    try stageImport(source, to: payload, skipped: &skipped)
    try validateHostTree(payload)
    let id = try backend.ready(name)
    let destination = "/workspace/" + source.lastPathComponent
    let guestTemporary = "/tmp/mybox-import-" + UUID().uuidString
    _ = try backend.run(["exec", id, "/bin/mkdir", "-m", "700", "--", guestTemporary], capture: true)
    defer { _ = try? backend.run(["exec", id, "/bin/rm", "-rf", "--", guestTemporary], capture: true) }
    _ = try backend.run(["cp", payload.path, "\(id):\(guestTemporary)/payload"])
    let move = """
    set -eu
    test -d /workspace && test ! -L /workspace
    if [ -e "$2" ] || [ -L "$2" ]; then
        printf '%s\\n' 'Import destination already exists; choose another folder or rename it inside the box.' >&2
        exit 1
    fi
    mv -T --no-clobber -- "$1" "$2"
    if [ -e "$1" ] || [ -L "$1" ]; then
        printf '%s\\n' 'Import destination already exists; nothing was replaced.' >&2
        exit 1
    fi
    """
    _ = try backend.run(["exec", id, "/bin/sh", "-c", move, "mybox-import", guestTemporary + "/payload", destination], capture: true)
    print("Imported to \(name):\(destination)")
    if skipped > 0 { print("Skipped \(skipped) symlinks or special files.") }
}

func workspacePath(_ value: String) throws -> String {
    guard !value.isEmpty, !value.utf8.contains(0) else { throw BoxError("Choose a path inside /workspace.") }
    let components = value.split(separator: "/").map(String.init)
    guard !components.contains("..") else { throw BoxError("Export paths cannot contain '..'.") }
    let clean = components.filter { $0 != "." }.joined(separator: "/")
    let path = value.hasPrefix("/") ? "/" + clean : "/workspace" + (clean.isEmpty ? "" : "/" + clean)
    guard path == "/workspace" || path.hasPrefix("/workspace/") else { throw BoxError("Export paths must be inside /workspace.") }
    return path
}

func exportFiles(_ name: String, path: String, destination: URL, backend: Backend) throws {
    let source = try workspacePath(path)
    guard !exists(destination) else { throw BoxError("Export destination already exists. Choose a new file or folder path.") }
    var isDirectory: ObjCBool = false
    guard files.fileExists(atPath: destination.deletingLastPathComponent().path, isDirectory: &isDirectory), isDirectory.boolValue else {
        throw BoxError("The export destination's parent folder must already exist.")
    }
    let id = try backend.ready(name)
    let validate = """
    set -eu
    current="$1"
    while [ "$current" != / ]; do
        if [ -L "$current" ]; then
            printf '%s\\n' 'Export refuses symlink paths. Choose the actual file or folder inside /workspace.' >&2
            exit 1
        fi
        current=${current%/*}
        [ -n "$current" ] || current=/
    done
    if [ ! -f "$1" ] && [ ! -d "$1" ]; then
        printf '%s\\n' 'Export source is missing or is not a regular file or folder.' >&2
        exit 1
    fi
    special=$(find "$1" ! -type f ! -type d -print -quit)
    if [ -n "$special" ]; then
        printf '%s\\n' 'Export refuses folders containing symlinks or special files. Export the required regular files or a source-only folder.' >&2
        exit 1
    fi
    """
    _ = try backend.run(["exec", id, "/bin/sh", "-c", validate, "mybox-export", source], capture: true)
    let temporary = try TemporaryFolder()
    defer { withExtendedLifetime(temporary) {} }
    let payload = temporary.url.appendingPathComponent("payload")
    _ = try backend.run(["cp", "\(id):\(source)", payload.path])
    try validateHostTree(payload)
    try files.moveItem(at: payload, to: destination)
    print("Exported to \(destination.path)")
}

let help = """
MyBox 0.1.0 — a small Linux workspace on your Mac

Usage: mybox <command>

  create <name> [--inside-port N --outside-port N] [--cpus N] [--memory 2g]
                         Create and start a Debian box (default: 2 CPUs, 2 GiB).
                         --inside-port: server port inside the Linux box.
                         --outside-port: localhost port on your Mac.
                         Repeat both flags for multiple TCP port mappings;
                         pair the first inside port with the first outside port.
  enter <name>           Start if needed and open Bash in /workspace.
  enter <name> -- <command> [arguments...]
                         Run a Linux command; piped input is supported.
  import <name> <host-path>
                         Copy a file or whole folder to /workspace/<source-name>.
                         Never replace an existing destination.
                         Start the box if it is stopped.
                         Include all file names, including hidden files.
                         Skip symlinks and special files.
  export <name> <box-path> <new-host-path>
                         Copy a file or whole folder out of /workspace.
                         Never replace an existing destination.
                         Start the box if it is stopped.
                         Relative box paths start at /workspace. Reject links.
  start <name>           Start a stopped box, keeping its files.
  stop <name>            Stop a box, keeping its files.
  list                   List boxes created by this CLI, including stopped ones.
  setup-network          Install the PF helper and automatic Mac startup setup.
  help                   Show this help.
  --version              Show the MyBox version.

No host folders, project disks, or SSH agents are mounted.
New boxes use a dedicated network. PF protection is checked before startup.
Run setup-network once in a Mac terminal. PF changes require administrator access.
"""

func mainCommand(_ arguments: [String]) throws {
    guard let command = arguments.first else { print(help); return }
    let rest = Array(arguments.dropFirst())
    if ["help", "--help", "-h"].contains(command) { print(help); return }
    if command == "--version" { print("MyBox 0.1.0"); return }
    #if MYBOX_TESTING
    if command == "_test-auth" {
        _ = try hostCommand("/usr/bin/sudo", ["-v"], terminal: true)
        return
    }
    #endif
    switch command {
    case "setup-network":
        guard rest.isEmpty else { throw BoxError("Usage: mybox setup-network") }
        try setupNetwork()
    case "create":
        let options = try CreateOptions(rest)
        try create(options, backend: Backend())
    case "list":
        guard rest.isEmpty else { throw BoxError("Usage: mybox list") }
        let records = try Backend().records(["list", "--all", "--format", "json"]).filter(\.isManaged).sorted { $0.id < $1.id }
        if records.isEmpty { print("No MyBox boxes yet. Create one with: mybox create demo"); return }
        let width = max(4, records.map { $0.id.count - namePrefix.count }.max() ?? 4) + 2
        print("NAME".padding(toLength: width, withPad: " ", startingAt: 0) + "STATE".padding(toLength: 12, withPad: " ", startingAt: 0) + "PORTS")
        for record in records {
            print(String(record.id.dropFirst(namePrefix.count)).padding(toLength: width, withPad: " ", startingAt: 0)
                + record.state.padding(toLength: 12, withPad: " ", startingAt: 0) + record.portDescription)
        }
    case "start", "stop":
        guard rest.count == 1 else { throw BoxError("Usage: mybox \(command) <name>") }
        _ = try containerID(rest[0])
        let backend = try Backend()
        let targetState = command == "start" ? "running" : "stopped"
        if command == "start" { _ = try backend.ready(rest[0]) }
        else { try backend.protectedStop(rest[0]) }
        print("\(rest[0]) is \(targetState).")
    case "enter":
        guard let name = rest.first else { throw BoxError("Usage: mybox enter <name> [-- <command> ...]") }
        _ = try containerID(name)
        guard rest.count == 1 || (rest.count >= 3 && rest[1] == "--") else { throw BoxError("Put '--' before a Linux command: mybox enter <name> -- <command> ...") }
        let backend = try Backend()
        let id = try backend.ready(name)
        var execArguments = ["exec", "--interactive", "--workdir", "/workspace"]
        if isatty(STDIN_FILENO) == 1 && isatty(STDOUT_FILENO) == 1 {
            execArguments += ["--tty", "--env", "TERM=" + (ProcessInfo.processInfo.environment["TERM"] ?? "xterm-256color")]
        }
        execArguments.append(id)
        execArguments += rest.count == 1 ? ["/bin/bash"] : Array(rest.dropFirst(2))
        try backend.replaceProcess(execArguments)
    case "import":
        guard rest.count == 2 else { throw BoxError("Usage: mybox import <name> <host-file-or-folder>") }
        _ = try containerID(rest[0])
        try importFiles(rest[0], path: rest[1], backend: Backend())
    case "export":
        guard rest.count == 3 else { throw BoxError("Usage: mybox export <name> <box-path> <new-host-path>") }
        _ = try containerID(rest[0])
        _ = try workspacePath(rest[1])
        try exportFiles(rest[0], path: rest[1], destination: hostURL(rest[2]), backend: Backend())
    default:
        throw BoxError("Unknown command \(command.debugDescription). Run mybox help for usage.")
    }
}

@main
struct MyBoxMain {
    static func main() {
        do {
            try MyBoxMain.run()
        } catch let error as CommandFailed {
            exit(error.status > 0 && error.status < 256 ? error.status : 1)
        } catch {
            note("MyBox: \(error)")
            exit(1)
        }
    }
    static func run() throws { try mainCommand(Array(CommandLine.arguments.dropFirst())) }
}
