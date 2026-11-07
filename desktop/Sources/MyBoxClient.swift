import AppKit
import Foundation

enum MyBoxClient {
    static func resolveExecutable() -> String? {
        let bundled = Bundle.main.url(forResource: "cli-location", withExtension: "txt")
            .flatMap { try? String(contentsOf: $0, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) }
        let candidates = [bundled, "/usr/local/bin/mybox", "/opt/homebrew/bin/mybox"].compactMap { $0 }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static func list(executable: String) async throws -> [Workspace] {
        try await Task.detached(priority: .utility) {
            try WorkspaceParser.parse(capture(executable: executable, arguments: ["list"]))
        }.value
    }

    /// Separate temporary files avoid stdout/stderr pipe deadlocks and keep
    /// diagnostics out of the data parser. Only read-only commands are captured.
    static func capture(executable: String, arguments: [String], timeout: TimeInterval = 20) throws -> String {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mybox-read-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let out = directory.appendingPathComponent("stdout"), err = directory.appendingPathComponent("stderr")
        FileManager.default.createFile(atPath: out.path, contents: nil, attributes: [.posixPermissions: 0o600])
        FileManager.default.createFile(atPath: err.path, contents: nil, attributes: [.posixPermissions: 0o600])
        let stdout = try FileHandle(forWritingTo: out), stderr = try FileHandle(forWritingTo: err)
        defer { try? stdout.close(); try? stderr.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout
        process.standardError = stderr
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment
        try process.run()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning {
            process.terminate()
            let grace = Date().addingTimeInterval(1)
            while process.isRunning && Date() < grace { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit()
            throw DesktopError("MyBox did not respond within \(Int(timeout)) seconds. Check the service, then try again.")
        }
        process.waitUntilExit()
        let output = try String(contentsOf: out, encoding: .utf8)
        let diagnostic = (try? String(contentsOf: err, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError(diagnostic.isEmpty ? "MyBox exited with status \(process.terminationStatus)." : diagnostic)
        }
        return output
    }

    @MainActor
    static func openTerminal(executable: String, arguments: [String]) throws {
        // Launch Services opens a private .command file in Terminal without an
        // Apple Events entitlement. Arguments are always POSIX shell quoted.
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mybox-command-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        let file = directory.appendingPathComponent("MyBox.command")
        let command = ShellCommand.line(executable: executable, arguments: arguments)
        let script = """
        #!/bin/sh
        export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
        /bin/rm -f -- \(ShellCommand.quote(file.path))
        /bin/rmdir -- \(ShellCommand.quote(directory.path))
        printf '\\033]0;MyBox\\007'
        \(command)
        mybox_exit=$?
        printf '\\nMyBox command finished (exit %s). You can close this window.\\n' "$mybox_exit"
        exit "$mybox_exit"
        """
        try script.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: file.path)
        let opener = Process()
        opener.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        opener.arguments = ["-a", "Terminal", file.path]
        try opener.run()
        opener.waitUntilExit()
        guard opener.terminationStatus == 0 else {
            try? FileManager.default.removeItem(at: directory)
            throw DesktopError("Terminal could not open the MyBox command.")
        }
    }
}
