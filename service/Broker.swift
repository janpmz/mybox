import Darwin
import Foundation

let brokerCommands: Set<String> = ["list", "create", "copy", "start", "stop", "delete", "enter", "ports", "resize", "import", "export", "_complete-boxes"]

func validateServiceCommand(_ value: Any) throws -> [String] {
    guard let args = value as? [String], let command = args.first, brokerCommands.contains(command),
          !args.contains(where: { $0.utf8.contains(0) }) else { throw BoxError("Unsupported MyBox command.") }
    if command == "import" {
        guard args.count == 3 else { throw BoxError("Usage: mybox import <box> <host-path>") }
        _ = try checkedTransferName(args[2])
    }
    if command == "export", args.count != 3 { throw BoxError("Usage: mybox export <box> <box-path> <new-host-path>") }
    return args
}

// Darwin's CMSG macros align to 32-bit boundaries, including on arm64.
private func controlAligned(_ size: Int) -> Int { (size + 3) & ~3 }
private let controlHeaderSize = controlAligned(MemoryLayout<cmsghdr>.size)

func sendDescriptors(_ socket: Int32, _ descriptors: [Int32]) throws {
    let size = controlHeaderSize + descriptors.count * MemoryLayout<Int32>.size
    let control = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: MemoryLayout<cmsghdr>.alignment)
    defer { control.deallocate() }
    control.initializeMemory(as: UInt8.self, repeating: 0, count: size)
    control.storeBytes(of: cmsghdr(cmsg_len: socklen_t(size), cmsg_level: SOL_SOCKET, cmsg_type: SCM_RIGHTS), as: cmsghdr.self)
    descriptors.withUnsafeBytes { control.advanced(by: controlHeaderSize).copyMemory(from: $0.baseAddress!, byteCount: $0.count) }
    var marker: UInt8 = 70
    let sent = withUnsafeMutablePointer(to: &marker) { byte in
        var vector = iovec(iov_base: UnsafeMutableRawPointer(byte), iov_len: 1)
        return withUnsafeMutablePointer(to: &vector) { vectors in
            var message = msghdr(msg_name: nil, msg_namelen: 0, msg_iov: vectors, msg_iovlen: 1,
                                 msg_control: control, msg_controllen: socklen_t(size), msg_flags: 0)
            return sendmsg(socket, &message, 0)
        }
    }
    guard sent == 1 else { throw systemError("Cannot pass MyBox command streams") }
}

func receiveDescriptors(_ socket: Int32) throws -> [OwnedFD] {
    let capacity = controlHeaderSize + 4 * MemoryLayout<Int32>.size
    let control = UnsafeMutableRawPointer.allocate(byteCount: capacity, alignment: MemoryLayout<cmsghdr>.alignment)
    defer { control.deallocate() }
    control.initializeMemory(as: UInt8.self, repeating: 0, count: capacity)
    var marker: UInt8 = 0, flags: Int32 = 0, length = 0
    let count = withUnsafeMutablePointer(to: &marker) { byte in
        var vector = iovec(iov_base: UnsafeMutableRawPointer(byte), iov_len: 1)
        return withUnsafeMutablePointer(to: &vector) { vectors in
            var message = msghdr(msg_name: nil, msg_namelen: 0, msg_iov: vectors, msg_iovlen: 1,
                                 msg_control: control, msg_controllen: socklen_t(capacity), msg_flags: 0)
            let result = recvmsg(socket, &message, 0)
            flags = message.msg_flags; length = Int(message.msg_controllen)
            return result
        }
    }
    var raw: [Int32] = [], offset = 0
    while offset + controlHeaderSize <= min(length, capacity) {
        let header = control.advanced(by: offset).load(as: cmsghdr.self)
        let bytes = Int(header.cmsg_len)
        guard bytes >= controlHeaderSize, offset + bytes <= min(length, capacity) else { break }
        if header.cmsg_level == SOL_SOCKET && header.cmsg_type == SCM_RIGHTS {
            for index in stride(from: controlHeaderSize, to: bytes - 3, by: 4) {
                raw.append(control.advanced(by: offset + index).load(as: Int32.self))
            }
        }
        offset += controlAligned(bytes)
    }
    guard count == 1, marker == 70, flags & MSG_CTRUNC == 0, raw.count == 4 else {
        raw.forEach { close($0) }; throw BoxError("Invalid command streams.")
    }
    var result: [OwnedFD] = []
    for (index, fd) in raw.enumerated() {
        do { result.append(try OwnedFD(fd)) }
        catch { raw.dropFirst(index + 1).forEach { close($0) }; throw error }
    }
    return result
}

func runServiceWorker(_ args: [String], _ descriptors: [OwnedFD], _ connection: Int32, _ worker: String) throws -> Int32 {
    let (local, remote) = try socketPair()
    var environment = ProcessInfo.processInfo.environment
    environment["MYBOX_FIREWALL_CHANNEL"] = String(remote.value)
    let child = try spawnServiceProcess(worker, args, streams: descriptors.prefix(3).map(\.value),
                                        inherited: [remote.value], environment: environment, newSession: true)
    var reaped = false
    defer {
        if !reaped {
            kill(-child, SIGTERM)
            var status: Int32 = 0
            let deadline = ProcessInfo.processInfo.systemUptime + 3
            while waitpid(child, &status, WNOHANG) == 0 && ProcessInfo.processInfo.systemUptime < deadline { usleep(10_000) }
            kill(-child, SIGKILL)
            _ = try? waitForChild(child)
        }
    }
    var pending = false, localOpen = true
    while true {
        var status: Int32 = 0
        let finished = waitpid(child, &status, WNOHANG)
        if finished == child { reaped = true; return childStatus(status) }
        if finished < 0 && errno != EINTR { throw systemError("Cannot wait for worker") }
        var pollers = [pollfd(fd: localOpen ? local.value : -1, events: Int16(POLLIN), revents: 0),
                       pollfd(fd: connection, events: Int16(POLLIN), revents: 0)]
        let ready = poll(&pollers, 2, 150)
        if ready < 0 && errno == EINTR { continue }
        guard ready >= 0 else { throw systemError("Cannot monitor worker") }
        if pollers[0].revents != 0 {
            var byte: UInt8 = 0
            if recv(local.value, &byte, 1, MSG_PEEK) == 0 { localOpen = false }
            else {
                guard !pending else { throw BoxError("Unexpected firewall request.") }
                let token = String(decoding: try readExact(local.value, 36), as: UTF8.self)
                guard UUID(uuidString: token)?.uuidString.lowercased() == token else { throw BoxError("Invalid firewall request.") }
                try sendPacket(connection, ["firewall", token]); pending = true
            }
        }
        if pollers[1].revents != 0 {
            guard let reply = try receivePacket(connection) as? [Any] else { throw BoxError("Unexpected client reply.") }
            if reply.count == 2, reply[0] as? String == "firewall", pending,
               let allowed = reply[1] as? NSNumber, CFGetTypeID(allowed) == CFBooleanGetTypeID() {
                pending = false; try writeAll(local.value, Data([allowed.boolValue ? 89 : 78]))
            } else if let strings = reply as? [String], strings == ["signal", "WINCH"] { kill(-child, SIGWINCH) }
            else if let strings = reply as? [String], strings == ["signal", "INT"] { kill(-child, SIGINT) }
            else { throw BoxError("Unexpected client reply.") }
        }
    }
}

func serveSession(_ connection: Int32, owner: uid_t, worker: String) throws {
    guard try peerUID(connection) == owner else { throw BoxError("This Mac account does not own the MyBox installation.") }
    var args = try validateServiceCommand(receivePacket(connection))
    let fds = try receiveDescriptors(connection)
    let temp = try TemporaryDirectory()
    defer { withExtendedLifetime(temp) {}; withExtendedLifetime(fds) {} }
    let payload = temp.path + "/payload"
    if args[0] == "import" {
        let unpacked = try unpackTransfer(fds[3].value, temp.path)
        let directory = temp.path + "/input"
        guard mkdir(directory, 0o700) == 0, rename(unpacked, directory + "/" + args[2]) == 0 else { throw systemError("Cannot stage import") }
        args[2] = directory + "/" + args[2]
    } else if args[0] == "export" { args.append(payload) }
    let status = try runServiceWorker(args, fds, connection, worker)
    if status == 0 && args[0] == "export" { try packTransfer(payload, fds[3].value) }
    try sendPacket(connection, ["exit", status])
}

func serveBroker() throws {
    let uid = try serviceUID()
    var home = stat()
    guard getuid() == uid, geteuid() == uid,
          let account = getpwnam(privateServiceAccount), String(cString: account.pointee.pw_dir) == privateServiceHome,
          lstat(privateServiceHome, &home) == 0, home.st_mode & S_IFMT == S_IFDIR,
          home.st_mode & 0o7777 == 0o700, home.st_uid == uid else { throw BoxError("The box service requires its dedicated non-admin account and private home.") }
    let session = try serviceCommand("/bin/launchctl", ["managername"])
    guard session.status == 0, String(decoding: session.output, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) == "Background" else {
        throw BoxError("The box service needs its own macOS background user session.")
    }
    guard let owner = UInt32(try String(contentsOfFile: helperDirectory + "/owner", encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)), owner > 0 else { throw BoxError("Invalid installation owner.") }
    umask(0o077)
    if unlink(brokerSocket) != 0 && errno != ENOENT { throw systemError("Cannot remove old MyBox socket") }
    let listener = try OwnedFD(socket(AF_UNIX, SOCK_STREAM, 0))
    var address = try socketAddress(brokerSocket)
    let bound = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(listener.value, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    guard bound == 0, chmod(brokerSocket, 0o666) == 0, listen(listener.value, 32) == 0 else { throw systemError("Cannot start MyBox listener") }
    signal(SIGCHLD, SIG_IGN)
    while true {
        let accepted = accept(listener.value, nil, nil)
        if accepted < 0 && errno == EINTR { continue }
        let conn = try OwnedFD(accepted)
        // One disconnected client must not terminate the background listener.
        do {
            try configureSocket(conn.value)
            if try peerUID(conn.value) == owner {
                _ = try spawnServiceProcess(helperDirectory + "/mybox-service", ["session", String(conn.value), String(owner), helperDirectory + "/mybox-worker"], inherited: [conn.value])
            }
        } catch { continue }
    }
}

// The handler only writes a byte; framed JSON is written by the main loop.
private var signalWakeFD: Int32 = -1
private func brokerSignal(_ number: Int32) {
    let saved = errno
    var byte = UInt8(number)
    _ = write(signalWakeFD, &byte, 1)
    errno = saved
}

func brokerClient(_ input: [String], socketPath: String = brokerSocket, expectedPeer: uid_t? = nil) throws -> Int32 {
    var args = input
    guard !args.isEmpty else { throw BoxError("Missing MyBox command.") }
    let spool = try temporaryFile()
    let connection: OwnedFD
    do { connection = try connectSocket(socketPath) }
    catch { throw BoxError("The private MyBox service is unavailable. Run mybox setup-service in your Mac terminal.") }
    let expected = try expectedPeer ?? serviceUID()
    guard try peerUID(connection.value) == expected else { throw BoxError("The connection did not reach the private MyBox account.") }
    var destination: String?, skipped = 0
    func absolute(_ path: String) -> URL { URL(fileURLWithPath: (path as NSString).expandingTildeInPath).standardizedFileURL }
    if args[0] == "import", args.count == 3 {
        let path = absolute(args[2])
        guard path.resolvingSymlinksInPath().path != "/",
              path.resolvingSymlinksInPath() != FileManager.default.homeDirectoryForCurrentUser.resolvingSymlinksInPath() else {
            throw BoxError("Choose a project file or directory.")
        }
        args[2] = try checkedTransferName(path.lastPathComponent)
        skipped = try packTransfer(path.path, spool.value, skipSpecial: true)
        lseek(spool.value, 0, SEEK_SET)
    }
    if args[0] == "export" {
        guard args.count == 4 else { throw BoxError("Usage: mybox export <box> <box-path> <new-host-path>") }
        let path = absolute(args.removeLast())
        var info = stat(), parent = stat()
        guard lstat(path.path, &info) != 0, errno == ENOENT,
              stat(path.deletingLastPathComponent().path, &parent) == 0, parent.st_mode & S_IFMT == S_IFDIR else {
            throw BoxError("Choose a new destination in an existing directory.")
        }
        destination = path.path
    }
    _ = try validateServiceCommand(args)
    try sendPacket(connection.value, args)
    try sendDescriptors(connection.value, [0, 1, 2, spool.value])
    let (events, wake) = try socketPair()
    guard fcntl(wake.value, F_SETFL, O_NONBLOCK) == 0 else { throw systemError("Cannot configure signal channel") }
    signalWakeFD = wake.value
    let oldINT = signal(SIGINT, brokerSignal), oldWINCH = signal(SIGWINCH, brokerSignal)
    defer { signal(SIGINT, oldINT); signal(SIGWINCH, oldWINCH); signalWakeFD = -1 }
    while true {
        var pollers = [pollfd(fd: connection.value, events: Int16(POLLIN), revents: 0), pollfd(fd: events.value, events: Int16(POLLIN), revents: 0)]
        if poll(&pollers, 2, -1) < 0 { if errno == EINTR { continue }; throw systemError("Cannot monitor MyBox command") }
        if pollers[1].revents != 0 {
            var bytes = [UInt8](repeating: 0, count: 256)
            let count = read(events.value, &bytes, bytes.count)
            for byte in bytes.prefix(max(0, count)) {
                try sendPacket(connection.value, ["signal", byte == SIGINT ? "INT" : "WINCH"])
            }
        }
        if pollers[0].revents == 0 { continue }
        guard let message = try receivePacket(connection.value) as? [Any], message.count == 2, let kind = message[0] as? String else { throw BoxError("Unexpected service reply.") }
        if kind == "exit", let status = jsonInteger(message[1]), (0...255).contains(status) {
            if status == 0, let destination {
                lseek(spool.value, 0, SEEK_SET)
                let temp = try TemporaryDirectory(parent: URL(fileURLWithPath: destination).deletingLastPathComponent().path)
                defer { withExtendedLifetime(temp) {} }
                try publishTransfer(unpackTransfer(spool.value, temp.path), destination)
                print("Exported to", destination)
            }
            if status == 0 && skipped > 0 { print("Skipped \(skipped) symlinks or special files.") }
            return Int32(status)
        }
        if kind == "error", let detail = message[1] as? String { throw BoxError(detail) }
        guard kind == "firewall", let token = message[1] as? String, UUID(uuidString: token)?.uuidString.lowercased() == token else { throw BoxError("Unexpected service reply.") }
        let null = try OwnedFD(open("/dev/null", O_RDWR | O_CLOEXEC))
        let child = try spawnServiceProcess("/usr/bin/sudo", [firewallProgram, "approve", token], streams: [null.value, null.value, 2])
        try sendPacket(connection.value, ["firewall", try waitForChild(child) == 0])
    }
}
