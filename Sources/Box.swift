import Foundation
import Darwin

let managedLabel = "io.github.janpmz.my-container-box.managed"
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

final class Backend {
    let executable: String
    private var needsServiceCheck: Bool
    var usesFork: Bool {
        #if MYBOX_TESTING
        if ProcessInfo.processInfo.environment["MYBOX_TEST_BRIDGE"] != nil { return true }
        #endif
        #if MYBOX_SERVICE_WORKER
        return true
        #else
        return false
        #endif
    }

    init(startServiceIfNeeded: Bool = true) throws {
        #if MYBOX_SERVICE_WORKER
        guard files.isExecutableFile(atPath: forkBridgeExecutable) else {
            throw BoxError("Install the shared my-container-box package before using MyBox.")
        }
        let candidates = [forkInstallRoot + "/bin/container"]
        #else
        let directories = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let candidates = directories.filter { $0.hasPrefix("/") }.map { $0 + "/container" }
            + ["/usr/local/bin/container", "/opt/homebrew/bin/container"]
        #endif
        guard let path = candidates.first(where: { files.isExecutableFile(atPath: $0) }) else {
            throw BoxError("Apple's container CLI was not found. It must be installed before using MyBox.")
        }
        executable = path
        needsServiceCheck = startServiceIfNeeded
    }

    private func serviceIsRunning() throws -> Bool {
        let result = try capturedCommand(executable, ["system", "status", "--format", "json"])
        if result.status == 0 {
            #if MYBOX_SERVICE_WORKER
            let metadata = try JSONSerialization.jsonObject(with: Data(result.output.utf8)) as? [String: Any]
            let paths = metadata?["paths"] as? [String: String]
            guard let appRoot = paths?["appRoot"],
                  URL(fileURLWithPath: appRoot).resolvingSymlinksInPath().standardizedFileURL.path ==
                    containerStorageRoot.deletingLastPathComponent().resolvingSymlinksInPath().standardizedFileURL.path else {
                throw BoxError("Apple's service is using an unexpected storage directory. MyBox stopped.")
            }
            guard let root = paths?["installRoot"],
                  URL(fileURLWithPath: root).standardizedFileURL.path == forkInstallRoot else {
                throw BoxError("The _mybox container service must use the shared installation in /usr/local.")
            }
            #endif
            return true
        }
        let data = Data(result.output.utf8)
        if result.status == 1, let payload = try? JSONDecoder().decode([String: String].self, from: data),
           let status = payload["status"], ["unregistered", "not running"].contains(status) { return false }
        throw BoxError("Cannot check Apple's container service. \(result.output.trimmingCharacters(in: .whitespacesAndNewlines))")
    }

    private func ensureService() throws {
        guard try !serviceIsRunning() else { return }
        let lock = try operationLock("service")
        defer { withExtendedLifetime(lock) {} }
        guard try !serviceIsRunning() else { return }
        note("Starting Apple's container service...")
        #if MYBOX_SERVICE_WORKER
        let kernelOption = "--enable-kernel-install"
        #else
        let kernelOption = "--disable-kernel-install"
        #endif
        let result = try capturedCommand(executable, ["system", "start", kernelOption])
        guard result.status == 0 else {
            throw BoxError("Cannot start Apple's container service. \(result.output.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
    }

    @discardableResult
    func run(_ arguments: [String], capture: Bool = false) throws -> Data {
        if needsServiceCheck {
            try ensureService()
            needsServiceCheck = false
        }
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

    func managedNames() throws -> [String] {
        try records(["list", "--all", "--format", "json"])
            .filter(\.isManaged).map { String($0.id.dropFirst(namePrefix.count)) }.sorted()
    }

    func resolveEnterName(_ query: String) throws -> String {
        _ = try containerID(query)
        let names = try managedNames()
        if names.contains(query) { return query }
        let candidates = names.filter { $0.contains(query) }
        guard !candidates.isEmpty else {
            throw BoxError("No MyBox name contains \(query.debugDescription). Run mybox list to see available boxes.")
        }
        guard candidates.count == 1 else {
            throw BoxError("Several MyBox names contain \(query.debugDescription):\n"
                + candidates.map { "  \($0)" }.joined(separator: "\n")
                + "\nType more of the name or use the full name.")
        }
        return candidates[0]
    }

    func ready(_ name: String, progress: ((String) -> Void)? = nil) throws -> String {
        try protectedReady(name, progress: progress)
    }
}

// macOS labels binary memory sizes and decimal disk sizes in GB.
let bytesPerMemoryGB: UInt64 = 1 << 30
let bytesPerDiskGB: UInt64 = 1_000_000_000

func formatSize(_ bytes: UInt64, scale: UInt64, unit: String, decimals: Int = 1) -> String {
    let value = (Double(bytes) / Double(scale)).formatted(
        .number.grouping(.never).precision(.fractionLength(0...decimals)))
    return "\(value) \(unit)"
}

func memorySize(_ bytes: UInt64) -> String {
    formatSize(bytes, scale: bytesPerMemoryGB, unit: "GB", decimals: 2)
}

func diskSize(_ bytes: UInt64) -> String {
    let units: [(UInt64, String)] = [(1000 * bytesPerDiskGB, "TB"), (bytesPerDiskGB, "GB"), (1_000_000, "MB"), (1000, "kB")]
    for (scale, unit) in units where bytes >= scale {
        return formatSize(bytes, scale: scale, unit: unit)
    }
    return "\(bytes) B"
}

struct BoxRecord {
    let id: String
    let configuration: [String: Any]
    let status: [String: Any]
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
        self.status = status
        self.state = state
    }

    var isManaged: Bool {
        let labels = configuration["labels"] as? [String: String] ?? [:]
        return id.hasPrefix(namePrefix) && labels[managedLabel] == "true"
            && matches(String(id.dropFirst(namePrefix.count)), "^[a-z][a-z0-9-]{0,47}$")
    }

    var ports: [[String: Any]] { configuration["publishedPorts"] as? [[String: Any]] ?? [] }

    var coreDescription: String {
        guard let resources = savedResources,
              let cores = resources["cpus"] as? Int, cores > 0 else { return "-" }
        return String(cores)
    }

    var memoryDescription: String {
        guard let resources = savedResources,
              let bytes = resources["memoryInBytes"] as? UInt64, bytes > 0 else { return "-" }
        return memorySize(bytes)
    }

    var diskDescriptions: (used: String, capacity: String) {
        let directory = containerStorageRoot
        let disk = directory.appendingPathComponent(id, isDirectory: true).appendingPathComponent("rootfs.ext4")
        var metadata = stat()
        // The logical file size gives capacity. Allocated blocks give host usage.
        guard lstat(disk.path, &metadata) == 0,
              metadata.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              metadata.st_size > 0, metadata.st_blocks >= 0 else { return ("-", "-") }
        return (diskSize(UInt64(metadata.st_blocks) * 512), diskSize(UInt64(metadata.st_size)))
    }

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
        guard insidePorts.count <= 64 else { throw BoxError("A box supports at most 64 MyBox port mappings.") }
        ports = zip(insidePorts, outsidePorts).map { inside, outside in "127.0.0.1:\(outside):\(inside)" }
    }
}

func createStoppedBox(_ options: CreateOptions, backend: Backend, image: String = trustedImage) throws {
    let lock = try networkLock()
    defer { withExtendedLifetime(lock) {} }
    let id = try containerID(options.name)
    let network = "mybox-net-" + options.name
    // A qualified alias avoids changing Apple's global DNS domain or the
    // existing container IDs. Apple persists it and updates its IP on start.
    var arguments = ["create", "--name", id, "--label", "\(managedLabel)=true", "--label", "\(networkLabel)=\(network)",
                     "--cpus", options.cpus, "--memory", options.memory,
                     "--dns", "1.1.1.1", "--dns", "1.0.0.1", "--network", "\(network),alias=\(options.name).box.test"]
    arguments += ["--entrypoint", "/bin/sh", image, "-c", "mkdir -p /workspace && exec sleep infinity"]
    note("Creating the box network...")
    var networkArguments = ["network", "create", "--label", "\(managedLabel)=true"]
    if backend.usesFork {
        let subnets = try backend.unusedBoxSubnets()
        networkArguments += ["--option", "variant=reserved-on-demand", "--subnet", subnets.ipv4, "--subnet-v6", subnets.ipv6]
    }
    _ = try backend.run(networkArguments + [network], capture: true)
    do {
        note("Creating the Linux box...")
        _ = try backend.run(arguments, capture: true)
    }
    catch {
        _ = try? backend.run(["network", "delete", network], capture: true)
        throw error
    }
}

func copyBox(_ arguments: [String], backend: Backend) throws {
    guard arguments.count == 2 else { throw BoxError("Usage: mybox copy <source-name> <new-name>") }
    let sourceID = try containerID(arguments[0])
    var options = try CreateOptions([arguments[1]])
    let destinationID = try containerID(options.name)
    guard sourceID != destinationID else { throw BoxError("Use a different name for the copy.") }
    guard try !backend.records(["list", "--all", "--format", "json"]).contains(where: { $0.id == destinationID }) else {
        throw BoxError("A container named \(destinationID) already exists. Use a new name for the copy.")
    }
    let source = try backend.managed(arguments[0])
    guard ["running", "stopped"].contains(source.state) else {
        throw BoxError("The source box is \(source.state). Wait for its current operation to finish.")
    }
    guard let resources = source.savedResources,
          let cpus = resources["cpus"] as? Int, cpus > 0,
          let memory = resources["memoryInBytes"] as? UInt64, memory > 0, memory % (1 << 20) == 0 else {
        throw BoxError("Cannot read the source box's CPU and memory limits.")
    }
    options.cpus = String(cpus)
    options.memory = "\(memory / (1 << 20))m"
    // Keep the image as the new container's base. Never use it for the trusted helper.
    let image = "mybox-copy-\(UUID().uuidString.lowercased()):snapshot"
    note("Copying the Linux filesystem, including installed tools and saved credentials...")
    do {
        _ = try backend.run(["commit", source.id, image], capture: true)
        try createStoppedBox(options, backend: backend, image: image)
    } catch {
        // Keep the image if a failed backend request might still have created the box.
        if let records = try? backend.records(["list", "--all", "--format", "json"]),
           !records.contains(where: { $0.id == destinationID }) {
            do { _ = try backend.run(["image", "delete", "--force", image], capture: true) }
            catch { note("Could not remove the unused copy image: \(image)") }
        } else {
            note("The copy image remains available: \(image)")
        }
        throw error
    }
    print("Copied \(arguments[0]) to \(options.name). The new box is stopped.")
    print("Port mappings were not copied. Use mybox ports add to select new outside ports.")
    print("Enter with: mybox enter \(options.name)")
}

func create(_ options: CreateOptions, backend: Backend) throws {
    try installedFirewall()
    try createStoppedBox(options, backend: backend)
    do {
        if !options.ports.isEmpty {
            let record = try backend.managed(options.name)
            let mappings = options.ports.map { value -> PortMapping in
                let parts = value.split(separator: ":")
                return PortMapping(inside: Int(parts[2])!, outside: Int(parts[1])!)
            }
            try savePortConfiguration(record.freshPortConfiguration(mappings))
        }
        _ = try backend.ready(options.name, progress: note)
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
    #if !MYBOX_SERVICE_WORKER
    print("Exported to \(destination.path)")
    #endif
}

let help = """
MyBox 0.1.0 — a small Linux workspace on your Mac

Usage: mybox <command>

  create <name> [--inside-port N --outside-port N] [--cpus N] [--memory 2g]
                         Create and start a Debian box (default: 2 CPUs, --memory 2g).
                         --inside-port: server port inside the Linux box.
                         --outside-port: localhost port on your Mac.
                         Repeat both flags for multiple TCP port mappings;
                         pair the first inside port with the first outside port.
  enter <name>           Start if needed and open Bash in /workspace.
                         Accept a unique part of a name. Exact names take priority.
                         List matches and stop if the partial name is ambiguous.
  enter <name> -- <command> [arguments...]
                         Run a Linux command; piped input is supported.
  copy <source-name> <new-name>
                         Copy installed tools, settings, and files into a stopped box.
                         Include saved credentials. Keep the source's CPU and memory limits.
                         Use a separate network. Do not copy port mappings or running processes.
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
  resize [name]          Choose a stopped box and enter CPU, memory, and disk values.
                         Press Enter to keep a value.
  resize <name> [--cpus N] [--memory-gb N] [--disk-size-gb N]
                         Change resources on a stopped box. Keep its files and ports.
                         Enter sizes in GB, using the macOS units for memory and storage.
                         Show available Mac resources before applying changes.
                         Memory: at least 200 MB. Disk: 128 MiB to less than 16 TiB.
                         Disk changes copy the filesystem and need temporary Mac space.
  ports add <name> --inside-port N --outside-port N
                         Add a TCP mapping without restarting a running box.
  ports remove <name> --outside-port N
                         Remove a MyBox mapping without restarting the box.
  ports list <name>      Show saved mappings and forwarding status.
  list                   List boxes created by this CLI, including stopped ones.
                         Show CPU cores, memory, disk use, disk capacity, and ports.
  setup-service          Install the private MyBox account and register its service.
  setup-network          Install the PF helper and automatic Mac startup setup.
  completion zsh         Print the zsh script for Tab completion.
  help                   Show this help.
  --version              Show the MyBox version.

No host folders, project disks, or SSH agents get mounted.
Each box connects to its own virtual network. MyBox checks the firewall rules before starting Linux.
Run setup-service once in a Mac terminal. PF changes require administrator access.
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
    case "completion":
        guard rest == ["zsh"] else { throw BoxError("Usage: mybox completion zsh") }
        print(zshCompletion)
    case "_complete-boxes":
        guard rest.isEmpty else { throw BoxError("Usage: mybox _complete-boxes") }
        for name in try Backend(startServiceIfNeeded: false).managedNames() { print(name) }
    case "setup-network":
        guard rest.isEmpty else { throw BoxError("Usage: mybox setup-network") }
        try setupNetwork()
    case "create":
        let options = try CreateOptions(rest)
        try create(options, backend: Backend())
    case "copy":
        guard rest.count == 2 else { throw BoxError("Usage: mybox copy <source-name> <new-name>") }
        _ = try containerID(rest[0])
        _ = try CreateOptions([rest[1]])
        guard rest[0] != rest[1] else { throw BoxError("Use a different name for the copy.") }
        try copyBox(rest, backend: Backend())
    case "ports":
        let options = try PortOptions(rest)
        try Backend().changePorts(options)
    case "resize":
        if rest.count <= 1 {
            if let name = rest.first { _ = try containerID(name) }
            try Backend().promptResize(name: rest.first)
        } else {
            let options = try ResizeOptions(rest)
            try Backend().resize(options)
        }
    case "list":
        guard rest.isEmpty else { throw BoxError("Usage: mybox list") }
        let records = try Backend().records(["list", "--all", "--format", "json"]).filter(\.isManaged).sorted { $0.id < $1.id }
        if records.isEmpty { print("No MyBox boxes yet. Create one with: mybox create demo"); return }
        let rows = [["NAME", "STATE", "CORES", "MEMORY", "DISK USED", "DISK MAX", "PORTS"]] + (try records.map { record in
            let disk = record.diskDescriptions
            return [String(record.id.dropFirst(namePrefix.count)), record.state, record.coreDescription,
                    record.memoryDescription, disk.used, disk.capacity, try record.allPortDescription()]
        })
        let widths = rows[0].indices.map { column in rows.map { $0[column].count }.max() ?? 0 }
        for row in rows {
            print(row.indices.map { column in
                column == row.count - 1 ? row[column] : row[column].padding(toLength: widths[column] + 2, withPad: " ", startingAt: 0)
            }.joined())
        }
        print("\nCORES and MEMORY are configured limits. DISK USED is the Linux disk's space on your Mac.")
        print("DISK MAX is virtual capacity. Disk growth needs free space on your Mac.")
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
        let resolvedName = try backend.resolveEnterName(name)
        if resolvedName != name { note("Using box \(resolvedName).") }
        try approveBoxAccess("enter", box: resolvedName, arguments: Array(rest.dropFirst(2)))
        let id = try backend.ready(resolvedName)
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
        try approveBoxAccess("import", box: rest[0], arguments: [URL(fileURLWithPath: rest[1]).lastPathComponent])
        try importFiles(rest[0], path: rest[1], backend: Backend())
    case "export":
        guard rest.count == 3 else { throw BoxError("Usage: mybox export <name> <box-path> <new-host-path>") }
        _ = try containerID(rest[0])
        _ = try workspacePath(rest[1])
        try approveBoxAccess("export", box: rest[0], arguments: [rest[1]])
        try exportFiles(rest[0], path: rest[1], destination: hostURL(rest[2]), backend: Backend())
    default:
        throw BoxError("Unknown command \(command.debugDescription). Run mybox help for usage.")
    }
}

let serviceCommands: Set<String> = ["list", "_complete-boxes", "create", "copy", "start", "stop", "enter",
                                    "import", "export", "resize", "ports"]

// The installed worker asks over a service-private socket. There is no CLI flag
// or client response that can grant access.
func approveBoxAccess(_ action: String, box: String, arguments: [String]) throws {
    #if MYBOX_SERVICE_WORKER
    guard boxAccessApproval == nil else { throw BoxError("This worker already requested box access.") }
    boxAccessApproval = try BoxAccessApproval([action, box] + arguments)
    #endif
}

func execTool(_ path: String, _ arguments: [String]) throws -> Never {
    let argv = ([path] + arguments).map { strdup($0) } + [nil]
    defer { argv.forEach { free($0) } }
    _ = argv.withUnsafeBufferPointer { execv(path, $0.baseAddress!) }
    throw BoxError("Cannot start \(path): \(String(cString: strerror(errno)))")
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
    static func run() throws {
        let args = Array(CommandLine.arguments.dropFirst())
        #if MYBOX_TESTING
        try mainCommand(args)
        #elseif MYBOX_SERVICE_WORKER
        guard getuid() == (try privateAccountID()), geteuid() == getuid(),
              let fd = ProcessInfo.processInfo.environment["MYBOX_FIREWALL_CHANNEL"].flatMap(Int32.init),
              fcntl(fd, F_SETFD, FD_CLOEXEC) == 0,
              let command = args.first, serviceCommands.contains(command) else {
            throw BoxError("The private worker accepts box commands only from the MyBox service.")
        }
        try mainCommand(args)
        #else
        let checkoutService = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("service")
        if args.first == "setup-service" {
            guard args.count == 1 else {
                throw BoxError("Usage: mybox setup-service")
            }
            let installer = checkoutService.appendingPathComponent("install.py")
            try execTool("/usr/bin/sudo", ["/usr/bin/python3", "-I", "-B", installer.path])
        }
        if let command = args.first, serviceCommands.contains(command) {
            // The checkout client can still reach the installed account socket during a software upgrade.
            let clients = [serviceInstall + "/broker.py", checkoutService.appendingPathComponent("broker.py").path]
            guard let client = clients.first(where: { files.isReadableFile(atPath: $0) }) else {
                throw BoxError("Run mybox setup-service in your Mac terminal to configure the private account.")
            }
            try execTool("/usr/bin/python3", ["-I", "-B", client, "client"] + args)
        }
        try mainCommand(args)
        #endif
    }
}
