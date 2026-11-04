import Foundation
import Darwin

struct ToolResult {
    let status: Int32
    let output: String
}

@discardableResult
func hostCommand(_ path: String, _ arguments: [String], check: Bool = true, terminal: Bool = false) throws -> ToolResult {
    var executable = path
    var commandArguments = arguments
    #if MYBOX_TESTING
    if let fake = ProcessInfo.processInfo.environment["MYBOX_TEST_HOST"] {
        executable = fake
        commandArguments = [path] + arguments
    }
    #endif
    let result: ToolResult
    if terminal {
        result = try foregroundCommand(executable, commandArguments)
    } else {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = commandArguments
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        result = ToolResult(status: process.terminationStatus, output: String(decoding: data, as: UTF8.self))
    }
    if check && result.status != 0 {
        if terminal {
            // The child reports its error directly on stderr. Preserve its exit
            // status without adding an empty, duplicate "sudo failed" message.
            let message = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            if !message.isEmpty { note(message) }
            throw CommandFailed(status: result.status)
        }
        throw BoxError("\(path) failed: \(result.output.trimmingCharacters(in: .whitespacesAndNewlines))")
    }
    return result
}

func foregroundCommand(_ executable: String, _ arguments: [String]) throws -> ToolResult {
    // Foundation Process creates a separate process group on macOS. That can
    // leave sudo outside the terminal's foreground job, particularly when the
    // caller redirects stdin. Spawn in the existing group instead. sudo alone
    // reads /dev/tty and controls password echo. MyBox never reads that input.
    let terminal = open("/dev/tty", O_RDWR | O_NOCTTY | O_CLOEXEC)
    if terminal >= 0 {
        let foreground = tcgetpgrp(terminal)
        close(terminal)
        guard foreground == getpgrp() else {
            throw BoxError("Run MyBox in the foreground of your Mac terminal before administrator authentication.")
        }
    }
    let output = Pipe()
    var actions: posix_spawn_file_actions_t?
    var error = posix_spawn_file_actions_init(&actions)
    guard error == 0 else { throw BoxError("Cannot prepare the administrator command: \(String(cString: strerror(error)))") }
    defer { posix_spawn_file_actions_destroy(&actions) }
    error = posix_spawn_file_actions_adddup2(&actions, output.fileHandleForWriting.fileDescriptor, STDOUT_FILENO)
    if error == 0 { error = posix_spawn_file_actions_addclose(&actions, output.fileHandleForReading.fileDescriptor) }
    if error == 0 { error = posix_spawn_file_actions_addclose(&actions, output.fileHandleForWriting.fileDescriptor) }
    guard error == 0 else { throw BoxError("Cannot connect the administrator command: \(String(cString: strerror(error)))") }
    var child: pid_t = 0
    let pointers = ([executable] + arguments).map { strdup($0) } + [nil]
    defer { pointers.forEach { free($0) } }
    let environmentPointers = ProcessInfo.processInfo.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { environmentPointers.forEach { free($0) } }
    error = pointers.withUnsafeBufferPointer { buffer in
        environmentPointers.withUnsafeBufferPointer { environment in
            posix_spawn(&child, executable, &actions, nil, buffer.baseAddress!, environment.baseAddress!)
        }
    }
    guard error == 0 else { throw BoxError("Cannot start the administrator command: \(String(cString: strerror(error)))") }
    output.fileHandleForWriting.closeFile()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    var status: Int32 = 0
    while waitpid(child, &status, 0) == -1 {
        if errno == EINTR { continue }
        throw BoxError("Cannot wait for the administrator command.")
    }
    let signal = status & 0x7f
    let exitStatus = signal == 0 ? (status >> 8) & 0xff : 128 + signal
    return ToolResult(status: exitStatus, output: String(decoding: data, as: UTF8.self))
}
