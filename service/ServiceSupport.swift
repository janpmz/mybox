import Darwin
import Foundation
import CoreFoundation

let privateServiceAccount = "_mybox"
let privateServiceHome = "/private/var/db/mybox-runtime"
let helperDirectory = "/Library/PrivilegedHelperTools/io.github.janpmz.my-container-box.service"
let brokerSocket = "/private/var/db/mybox-socket/service.sock"
let firewallProgram = "/Library/PrivilegedHelperTools/mybox-firewall-manager"
let maximumPacket = 128 * 1024

func systemError(_ message: String) -> BoxError { BoxError("\(message): \(String(cString: strerror(errno)))") }

final class OwnedFD {
    let value: Int32
    init(_ value: Int32) throws {
        guard value >= 0 else { throw systemError("Cannot open MyBox stream") }
        guard fcntl(value, F_SETFD, FD_CLOEXEC) == 0 else { close(value); throw systemError("Cannot protect MyBox stream") }
        self.value = value
    }
    deinit { close(value) }
}

func writeAll(_ fd: Int32, _ data: Data) throws {
    try data.withUnsafeBytes { bytes in
        var offset = 0
        while offset < bytes.count {
            let count = write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw systemError("Cannot write MyBox stream") }
            offset += count
        }
    }
}

struct StreamClosed: Error {}
func readExact(_ fd: Int32, _ size: Int) throws -> Data {
    var data = Data(count: size)
    try data.withUnsafeMutableBytes { bytes in
        var offset = 0
        while offset < size {
            let count = read(fd, bytes.baseAddress!.advanced(by: offset), size - offset)
            if count < 0 && errno == EINTR { continue }
            if count == 0 && offset == 0 { throw StreamClosed() }
            guard count > 0 else { throw BoxError("Incomplete MyBox stream or expired connection.") }
            offset += count
        }
    }
    return data
}

func sendPacket(_ fd: Int32, _ value: Any) throws {
    let body = try JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed])
    guard body.count <= maximumPacket else { throw BoxError("The command is too long.") }
    var length = UInt32(body.count).bigEndian
    try writeAll(fd, Data(bytes: &length, count: 4) + body)
}

func receivePacket(_ fd: Int32, limit: Int = maximumPacket) throws -> Any {
    let size = try readExact(fd, 4).reduce(0) { ($0 << 8) | Int($1) }
    guard size > 0, size <= limit else { throw BoxError("Invalid MyBox record length.") }
    return try JSONSerialization.jsonObject(with: readExact(fd, size), options: [.fragmentsAllowed])
}

func jsonInteger(_ value: Any) -> Int64? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: number.objCType)),
          let integer = Int64(number.stringValue) else { return nil }
    return integer
}

func peerUID(_ fd: Int32) throws -> uid_t {
    var uid = uid_t.max, gid = gid_t.max
    guard getpeereid(fd, &uid, &gid) == 0 else { throw systemError("Cannot identify MyBox peer") }
    return uid
}

func serviceUID() throws -> uid_t {
    guard let account = getpwnam(privateServiceAccount), account.pointee.pw_uid > 0 else {
        throw BoxError("The private MyBox account is unavailable. Run mybox setup-service in your Mac terminal.")
    }
    return account.pointee.pw_uid
}

func configureSocket(_ fd: Int32) throws {
    var yes: Int32 = 1
    guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout.size(ofValue: yes))) == 0 else {
        throw systemError("Cannot configure MyBox connection")
    }
}

func socketTimeout(_ fd: Int32, _ seconds: Double) throws {
    guard seconds > 0 else { throw BoxError("MyBox approval expired.") }
    var timeout = timeval(tv_sec: Int(seconds), tv_usec: Int32((seconds - floor(seconds)) * 1_000_000))
    for option in [SO_RCVTIMEO, SO_SNDTIMEO] {
        guard setsockopt(fd, SOL_SOCKET, option, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout))) == 0 else {
            throw systemError("Cannot set MyBox timeout")
        }
    }
}

func socketAddress(_ path: String) throws -> sockaddr_un {
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    let bytes = Array(path.utf8) + [0]
    guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw BoxError("MyBox socket path is too long.") }
    withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
    return address
}

func connectSocket(_ path: String) throws -> OwnedFD {
    let fd = try OwnedFD(socket(AF_UNIX, SOCK_STREAM, 0))
    try configureSocket(fd.value)
    var address = try socketAddress(path)
    let result = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd.value, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    guard result == 0 else { throw systemError("Cannot connect to MyBox") }
    return fd
}

func socketPair() throws -> (OwnedFD, OwnedFD) {
    var descriptors: [Int32] = [-1, -1]
    guard socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors) == 0 else { throw systemError("Cannot create MyBox channel") }
    let first: OwnedFD
    do { first = try OwnedFD(descriptors[0]) }
    catch { close(descriptors[1]); throw error }
    let second = try OwnedFD(descriptors[1])
    try configureSocket(first.value); try configureSocket(second.value)
    return (first, second)
}

final class TemporaryDirectory {
    let path: String
    init(parent: String = NSTemporaryDirectory()) throws {
        var template = Array((parent + "/mybox-XXXXXX").utf8CString)
        guard mkdtemp(&template) != nil else { throw systemError("Cannot create temporary directory") }
        path = String(cString: template)
    }
    deinit { try? FileManager.default.removeItem(atPath: path) }
}

func temporaryFile(parent: String = NSTemporaryDirectory()) throws -> OwnedFD {
    var template = Array((parent + "/mybox-XXXXXX").utf8CString)
    let fd = mkstemp(&template)
    guard fd >= 0 else { throw systemError("Cannot create temporary file") }
    unlink(template)
    return try OwnedFD(fd)
}

// posix_spawn avoids forking a process that has already used macOS frameworks.
// Only explicitly listed descriptors survive exec. Worker processes get a new
// session so terminal signals can be forwarded to their whole process group.
func spawnServiceProcess(_ executable: String, _ arguments: [String],
                         streams: [Int32] = [0, 1, 2], inherited: [Int32] = [],
                         environment: [String: String] = ProcessInfo.processInfo.environment,
                         newSession: Bool = false) throws -> pid_t {
    var actions: posix_spawn_file_actions_t?, attributes: posix_spawnattr_t?
    func check(_ code: Int32) throws { if code != 0 { throw BoxError("Cannot start MyBox helper: \(String(cString: strerror(code)))") } }
    try check(posix_spawn_file_actions_init(&actions))
    defer { posix_spawn_file_actions_destroy(&actions) }
    try check(posix_spawnattr_init(&attributes))
    defer { posix_spawnattr_destroy(&attributes) }
    var mask = sigset_t(), defaults = sigset_t()
    sigemptyset(&mask); sigemptyset(&defaults)
    for signal in [SIGINT, SIGWINCH, SIGCHLD, SIGPIPE, SIGTERM] { sigaddset(&defaults, signal) }
    try check(posix_spawnattr_setsigmask(&attributes, &mask))
    try check(posix_spawnattr_setsigdefault(&attributes, &defaults))
    try check(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF | (newSession ? POSIX_SPAWN_SETSID : 0))))
    // Duplicate first, above every target, to avoid source/target aliasing.
    var copies: [OwnedFD] = []
    for (target, source) in streams.enumerated() {
        let copy = try OwnedFD(fcntl(source, F_DUPFD_CLOEXEC, 64))
        copies.append(copy)
        try check(posix_spawn_file_actions_adddup2(&actions, copy.value, Int32(target)))
    }
    for fd in inherited { try check(posix_spawn_file_actions_addinherit_np(&actions, fd)) }
    let argv = ([executable] + arguments).map { strdup($0) } + [nil]
    let env = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { argv.forEach { free($0) }; env.forEach { free($0) }; withExtendedLifetime(copies) {} }
    var child: pid_t = 0
    try argv.withUnsafeBufferPointer { args in
        try env.withUnsafeBufferPointer { values in
            try check(posix_spawn(&child, executable, &actions, &attributes, args.baseAddress!, values.baseAddress!))
        }
    }
    return child
}

func childStatus(_ status: Int32) -> Int32 { status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f) }
func waitForChild(_ pid: pid_t) throws -> Int32 {
    var status: Int32 = 0
    while waitpid(pid, &status, 0) < 0 {
        if errno == EINTR { continue }
        throw systemError("Cannot wait for MyBox helper")
    }
    return childStatus(status)
}

struct ServiceCommandResult { let status: Int32; let output: Data; let error: Data }
struct ServiceTimeout: Error {}
func serviceCommand(_ executable: String, _ arguments: [String], input: Data = Data(), timeout: Double = 120) throws -> ServiceCommandResult {
    let source = try temporaryFile(), out = try temporaryFile(), err = try temporaryFile()
    try writeAll(source.value, input); lseek(source.value, 0, SEEK_SET)
    let pid = try spawnServiceProcess(executable, arguments, streams: [source.value, out.value, err.value])
    let deadline = ProcessInfo.processInfo.systemUptime + timeout
    var status: Int32 = 0
    while true {
        let waited = waitpid(pid, &status, WNOHANG)
        if waited == pid { break }
        if waited < 0 && errno != EINTR { throw systemError("Cannot wait for MyBox helper") }
        if ProcessInfo.processInfo.systemUptime >= deadline {
            kill(pid, SIGKILL); _ = try? waitForChild(pid); throw ServiceTimeout()
        }
        usleep(10_000)
    }
    lseek(out.value, 0, SEEK_SET); lseek(err.value, 0, SEEK_SET)
    return ServiceCommandResult(status: childStatus(status),
        output: FileHandle(fileDescriptor: out.value, closeOnDealloc: false).readDataToEndOfFile(),
        error: FileHandle(fileDescriptor: err.value, closeOnDealloc: false).readDataToEndOfFile())
}
