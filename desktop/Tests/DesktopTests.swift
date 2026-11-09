import AppKit
import Foundation

@main
struct DesktopTests {
    static func main() async throws {
        var checks = 0
        func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
            guard condition() else { fatalError(message) }
            checks += 1
        }
        func expectFailure(_ message: String, _ operation: () throws -> Void) {
            do { try operation(); fatalError(message) } catch { checks += 1 }
        }

        let json = """
        {"schemaVersion":1,"boxes":[
          {"name":"demo","state":"running","cpus":4,"memoryBytes":4294967296,
           "diskUsedBytes":2800000000,"diskCapacityBytes":20000000000,
           "ports":[{"inside":3000,"outside":8766},{"inside":8080,"outside":8080}]},
          {"name":"weekend-project","state":"stopped","cpus":2,"memoryBytes":268435456,
           "diskUsedBytes":null,"diskCapacityBytes":null,"ports":[]}
        ]}
        """
        let boxes = try WorkspaceParser.parse(json)
        expect(boxes.count == 2, "Expected both rows")
        expect(boxes[0].name == "demo" && boxes[0].isRunning, "Running state")
        expect(boxes[0].cores == "4" && boxes[0].memory == "4 GB", "Memory uses binary GB")
        expect(boxes[0].diskUsed.replacingOccurrences(of: ",", with: ".") == "2.8 GB" && boxes[0].diskCapacity == "20 GB", "Disk uses decimal GB")
        expect(boxes[0].ports == [PortMapping(inside: 3000, outside: 8766), PortMapping(inside: 8080, outside: 8080)], "Multiple numeric ports")
        expect(boxes[1].memory.replacingOccurrences(of: ",", with: ".") == "0.25 GB" && boxes[1].diskUsed == "-", "Fractional memory and unavailable disk")
        expect(!boxes[1].isRunning && boxes[1].ports.isEmpty, "Stopped state without ports")
        let empty = try WorkspaceParser.parse("{\"schemaVersion\":1,\"boxes\":[]}")
        expect(empty.isEmpty, "Empty workspace list")
        let unknown = try WorkspaceParser.parse(json.replacingOccurrences(of: "\"cpus\":4", with: "\"cpus\":null")
            .replacingOccurrences(of: "\"memoryBytes\":4294967296", with: "\"memoryBytes\":null"))
        expect(unknown[0].cores == "-" && unknown[0].memory == "-", "Unknown resources must not become zero")
        expect(boxes[0].memoryBytes == 4_294_967_296 && boxes[0].diskUsedBytes == 2_800_000_000, "Preserve exact byte values for summaries")
        expect(WorkspaceTotals(boxes: boxes).runningMemory == "4 GB", "Running RAM excludes stopped boxes")
        expect(WorkspaceTotals(boxes: boxes).diskUsed == "Unavailable", "Missing disk usage must not become a partial total")
        expect(WorkspaceTotals(boxes: unknown).runningMemory == "Unavailable", "Missing running RAM must remain unknown")
        expect(WorkspaceTotals(boxes: []).runningMemory == "0 GB" && WorkspaceTotals(boxes: []).diskUsed == "0 B", "Empty list has zero totals")
        expect(WorkspaceTotals(boxes: [boxes[1]]).runningMemory == "0 GB", "Stopped-only list has no assigned running RAM")
        expect(WorkspaceTotals(boxes: Workspace.examples).diskUsed.replacingOccurrences(of: ",", with: ".") == "5.5 GB", "Disk total includes stopped boxes")
        var largeBox = boxes[0]
        largeBox.memoryBytes = UInt64.max
        largeBox.diskUsedBytes = UInt64.max
        expect(WorkspaceTotals(boxes: [largeBox, boxes[0]]).runningMemory == "Unavailable", "Resource sum cannot overflow")
        let extended = try WorkspaceParser.parse(json.replacingOccurrences(of: "\"state\":\"running\"", with: "\"futureField\":true,\"state\":\"running\""))
        expect(extended == boxes, "Additional JSON fields must be tolerated")
        expectFailure("Malformed output must not become an empty list") { _ = try WorkspaceParser.parse("permission denied") }
        for invalid in ["{}", "[]", "{\"schemaVersion\":1}", "{\"schemaVersion\":2,\"boxes\":[]}",
                        json.replacingOccurrences(of: "\"name\":\"demo\",", with: ""),
                        json.replacingOccurrences(of: "\"cpus\":4", with: "\"cpus\":0"),
                        json.replacingOccurrences(of: "\"memoryBytes\":4294967296", with: "\"memoryBytes\":\"4 GB\""),
                        json.replacingOccurrences(of: "\"diskUsedBytes\":2800000000", with: "\"diskUsedBytes\":-1"),
                        json.replacingOccurrences(of: "\"outside\":8766", with: "\"outside\":70000"),
                        json.replacingOccurrences(of: "\"outside\":8766", with: "\"outside\":8080"),
                        json.replacingOccurrences(of: "weekend-project", with: "demo")] {
            expectFailure("Invalid JSON data must be rejected") { _ = try WorkspaceParser.parse(invalid) }
        }
        for name in ["", "Demo", "a;whoami", "../box", "a\n", "1box", String(repeating: "a", count: 49)] {
            expect(!WorkspaceParser.validName(name), "Invalid name accepted: \(name.debugDescription)")
        }
        expect(WorkspaceParser.validName("my-box-42"), "Valid box name")

        let cliDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("mybox-desktop-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: cliDirectory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: cliDirectory) }
        let cli = cliDirectory.appendingPathComponent("mybox")
        let script = "#!/bin/sh\n[ \"$#\" = 2 ] && [ \"$1\" = list ] && [ \"$2\" = --json ] || exit 2\nprintf '%s' " + ShellCommand.quote(json) + "\nprintf diagnostic >&2\n"
        try script.write(to: cli, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: cli.path)
        let fetched = try await MyBoxClient.list(executable: cli.path)
        expect(fetched == boxes, "Desktop must request --json and exclude stderr")
        try "#!/bin/sh\nprintf 'MyBox: Usage: mybox list\\n' >&2\nexit 1\n".write(to: cli, atomically: false, encoding: .utf8)
        do {
            _ = try await MyBoxClient.list(executable: cli.path)
            fatalError("Older service must not look like an empty list")
        } catch { expect(error.localizedDescription.contains("mybox setup-service"), "Explain how to update an older service") }

        let arguments = ["two words", "quote's", "\"double\"", "$(echo injected)", "`echo injected`", "line\nbreak", "; exit 31", "", "☃"]
        let command = ShellCommand.line(executable: "/usr/bin/printf", arguments: ["%s\\0"] + arguments)
        let echoed = try MyBoxClient.capture(executable: "/bin/sh", arguments: ["-c", command])
        expect(Array(echoed.components(separatedBy: "\0").dropLast()) == arguments, "Shell command changed argument boundaries or evaluated input")
        let separate = try MyBoxClient.capture(executable: "/bin/sh", arguments: ["-c", "printf data; printf diagnostic >&2"])
        expect(separate == "data", "Diagnostics leaked into parsed output")
        do {
            _ = try MyBoxClient.capture(executable: "/bin/sh", arguments: ["-c", "printf 'useful failure' >&2; exit 7"])
            fatalError("Nonzero status accepted")
        } catch { expect(error.localizedDescription == "useful failure", "Lost CLI failure message") }
        let start = Date()
        expectFailure("A hung CLI must time out") {
            _ = try MyBoxClient.capture(executable: "/bin/sleep", arguments: ["10"], timeout: 0.1)
        }
        expect(Date().timeIntervalSince(start) < 3, "Timeout failed to bound execution")

        // Exercise the actual Terminal script without opening Terminal or touching boxes.
        for (shellBody, expectedExit) in [("exit 0", 0), ("exit 7", 7), ("exit 129", 129)] {
            let commandDirectory = cliDirectory.appendingPathComponent("terminal's command")
            try FileManager.default.createDirectory(at: commandDirectory, withIntermediateDirectories: false)
            let commandFile = commandDirectory.appendingPathComponent("MyBox.command")
            let completion = commandDirectory.appendingPathComponent("exit-status")
            let terminalScript = MyBoxClient.terminalScript(executable: "/bin/sh", arguments: ["-c", shellBody], file: commandFile, completion: completion)
            try terminalScript.write(to: commandFile, atomically: true, encoding: .utf8)
            do {
                _ = try MyBoxClient.capture(executable: "/bin/sh", arguments: [commandFile.path])
                expect(expectedExit == 0, "Terminal script must preserve failures")
            } catch { expect(expectedExit != 0, "Terminal script must preserve success") }
            let exitStatus = try String(contentsOf: completion, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            expect(exitStatus == String(expectedExit), "Terminal completion must report the command's exit status")
            expect(!FileManager.default.fileExists(atPath: commandFile.path), "Terminal command script removes itself")
            try FileManager.default.removeItem(at: commandDirectory)
        }

        let folder = cliDirectory.appendingPathComponent("Project's $(echo injected) `echo bad`; files\n雪", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        let selectedFolder = try FolderInput.validate([folder])[0]
        expect(selectedFolder.path == folder.path, "Folder selection must preserve spaces, quotes, Unicode, and shell characters")
        let sourceFile = folder.appendingPathComponent("original.txt")
        try "Keep the original".write(to: sourceFile, atomically: false, encoding: .utf8)
        let folderLink = cliDirectory.appendingPathComponent("folder-link")
        try FileManager.default.createSymbolicLink(at: folderLink, withDestinationURL: folder)
        for invalid in [[], [sourceFile], [folderLink], [URL(string: "https://example.com/folder")!],
                        [URL(fileURLWithPath: "/")], [FileManager.default.homeDirectoryForCurrentUser],
                        [cliDirectory.appendingPathComponent("missing")]] {
            expectFailure("Only existing project folders may be selected") { _ = try FolderInput.validate(invalid) }
        }
        for (label, expected) in [("My Project", "my-project"), ("123 Project", "box-123-project"),
                                  ("Café", "cafe"), ("☃", "folder"), ("---", "folder")] {
            let suggestion = FolderInput.suggestedName(for: URL(fileURLWithPath: "/tmp/" + label), existing: [])
            expect(suggestion == expected, "Folder names must become valid editable box names")
            try BoxCommand.newName(suggestion, existing: [])
        }
        let longFolder = URL(fileURLWithPath: "/tmp/" + String(repeating: "a", count: 60))
        let firstName = FolderInput.suggestedName(for: longFolder, existing: [])
        let secondName = FolderInput.suggestedName(for: longFolder, existing: [firstName])
        let thirdName = FolderInput.suggestedName(for: longFolder, existing: [firstName, secondName])
        try BoxCommand.newName(thirdName, existing: [firstName, secondName])
        expect(firstName.count == 48 && secondName.count == 48 && thirdName.hasSuffix("-3"), "Name suggestions must handle collisions within the length limit")

        let secondFolder = cliDirectory.appendingPathComponent("Another folder", isDirectory: true)
        let thirdFolder = cliDirectory.appendingPathComponent("Third folder", isDirectory: true)
        try FileManager.default.createDirectory(at: secondFolder, withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: thirdFolder, withIntermediateDirectories: false)
        let selectedFolders = try FolderInput.validate([folder, secondFolder, folder, thirdFolder])
        expect(selectedFolders == [selectedFolder, secondFolder, thirdFolder], "Multiple folder selection preserves order and ignores repeated drops of the same folder")
        let sameName = secondFolder.appendingPathComponent(folder.lastPathComponent, isDirectory: true)
        try FileManager.default.createDirectory(at: sameName, withIntermediateDirectories: false)
        expectFailure("Distinct folders with the same destination name must be rejected before creation") {
            _ = try FolderInput.validate([folder, sameName])
        }
        expectFailure("A mixed drop cannot silently skip an invalid file") { _ = try FolderInput.validate([folder, sourceFile]) }

        // Run the real multi-folder script against a fake CLI, including failures
        // in the middle of copying. Never create or modify real boxes.
        for (createExit, failingIndex, importExit) in [(0, 0, 0), (7, 0, 0), (0, 0, 9), (0, 1, 9), (0, 1, 130), (0, 2, 9)] {
            let commandDirectory = cliDirectory.appendingPathComponent("folder-command")
            try FileManager.default.createDirectory(at: commandDirectory, withIntermediateDirectories: false)
            let commandFile = commandDirectory.appendingPathComponent("MyBox.command")
            let completion = commandDirectory.appendingPathComponent("exit-status")
            let calls = cliDirectory.appendingPathComponent("calls")
            let fakeCLI = """
            #!/bin/sh
            printf '%s\\0' "$@" >> \(ShellCommand.quote(calls.path))
            case "$1" in
              create) exit \(createExit);;
              import) if [ "$3" = \(ShellCommand.quote(selectedFolders[failingIndex].path)) ]; then exit \(importExit); fi;;
              *) exit 99;;
            esac
            exit 0
            """
            try fakeCLI.write(to: cli, atomically: false, encoding: .utf8)
            let createArguments = try BoxCommand.create(name: "from-folder", cpus: "4", memory: "0.5", ports: [], existing: [])
            let terminalScript = MyBoxClient.terminalScript(executable: cli.path, arguments: createArguments,
                                                           file: commandFile, completion: completion, importing: selectedFolders)
            try terminalScript.write(to: commandFile, atomically: false, encoding: .utf8)
            let expectedExit = createExit == 0 ? importExit : createExit
            do {
                _ = try MyBoxClient.capture(executable: "/bin/sh", arguments: [commandFile.path])
                expect(expectedExit == 0, "Folder workflow must preserve command failures")
            } catch {
                expect(expectedExit != 0, "Folder workflow must complete when both commands succeed")
                expect(error.localizedDescription.contains(createExit == 0 ? "Use Import on that box to copy the remaining folders" : "No folders were copied"),
                       "A failed workflow must explain whether the folder copy was attempted")
            }
            let actualCalls = try String(contentsOf: calls, encoding: .utf8).components(separatedBy: "\0").dropLast()
            let copiedFolders = createExit != 0 ? [] : Array(selectedFolders.prefix(importExit == 0 ? selectedFolders.count : failingIndex + 1))
            let expectedCalls = createArguments + copiedFolders.flatMap { ["import", "from-folder", $0.path] }
            expect(Array(actualCalls) == expectedCalls,
                   "Import must preserve each folder path, wait for creation, and stop after the first failed copy")
            let exitStatus = try String(contentsOf: completion, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            expect(exitStatus == String(expectedExit), "Completion must wait for the folder copy and preserve its failure status")
            expect(!FileManager.default.fileExists(atPath: commandFile.path), "Folder command script must clean up itself")
            try FileManager.default.removeItem(at: commandDirectory)
            try FileManager.default.removeItem(at: calls)
        }
        let original = try String(contentsOf: sourceFile, encoding: .utf8)
        expect(original == "Keep the original", "The desktop workflow must not move or modify the source folder")

        let create = try BoxCommand.create(name: "sample", cpus: "4", memory: "0.5",
            ports: [PortDraft(outside: "8766", inside: "3000"), PortDraft(outside: "8767", inside: "8080")], existing: [])
        expect(create == ["create", "sample", "--cpus", "4", "--memory", "512m", "--inside-port", "3000", "--outside-port", "8766", "--inside-port", "8080", "--outside-port", "8767"], "Creation must convert numeric GB to CLI memory units and preserve port pairs")
        expectFailure("Duplicate create port") {
            _ = try BoxCommand.create(name: "sample", cpus: "2", memory: "2", ports: [PortDraft(outside: "80", inside: "80"), PortDraft(outside: "80", inside: "90")], existing: [])
        }
        expectFailure("Existing box name") { try BoxCommand.newName("demo", existing: ["demo"]) }
        expectFailure("Trailing hyphen") { try BoxCommand.newName("demo-", existing: []) }
        expectFailure("Invalid CPU") { _ = try BoxCommand.create(name: "sample", cpus: "1.5", memory: "2", ports: [], existing: []) }
        expectFailure("Invalid memory units") { _ = try BoxCommand.create(name: "sample", cpus: "2", memory: "2GB", ports: [], existing: []) }
        expect((try? SizeInput.memoryArgument("2")) == "2048m", "Whole GB must retain the current default memory")
        expect((try? SizeInput.memoryArgument("0,5")) == "512m", "Accept decimal comma memory input")
        expect((try? SizeInput.memoryArgument("0.2")) == "205m", "Round fractional memory up like CLI resize")
        for invalid in ["2g", "512m", "0", "-2", "NaN", "1e3", "999999999999999999999"] {
            expectFailure("Invalid numeric memory accepted") { _ = try SizeInput.memoryArgument(invalid) }
        }
        expect(SizeInput.gigabytesPlaceholder("8 GB") == "8", "Memory placeholder must not contain units")
        expect(SizeInput.gigabytesPlaceholder("550,8 GB") == "550.8", "Accept localized resource values")
        expect(SizeInput.gigabytesPlaceholder("1.2 TB") == "1200", "Convert large disk placeholders to GB")
        expect(SizeInput.gigabytesPlaceholder("128 MB") == "0.128", "Convert small disk placeholders to GB")
        expect(SizeInput.gigabytesPlaceholder("-").isEmpty, "Unknown sizes must stay blank")
        expectFailure("Invalid port range") { _ = try PortDraft(outside: "0", inside: "3000").mapping() }
        expectFailure("Malformed numeric port") { _ = try PortDraft(outside: "8e3", inside: "3000").mapping() }
        var portSelection = PortSelection()
        portSelection.draft = PortDraft(outside: "8766", inside: "3000")
        expect(portSelection.confirmed.isEmpty && portSelection.hasDraft, "Typing a port mapping must not add it implicitly")
        try portSelection.add()
        expect(portSelection.confirmed.count == 1 && !portSelection.hasDraft, "Add confirms the mapping and clears both inputs")
        portSelection.draft = PortDraft(outside: "8766", inside: "8080")
        expectFailure("Explicit Add must reject duplicate outside ports") { try portSelection.add() }
        expect(portSelection.confirmed.count == 1 && portSelection.hasDraft, "Failed Add must preserve confirmed mappings and the user's input")
        portSelection.draft = PortDraft(outside: "8767", inside: "")
        expectFailure("Explicit Add requires both ports") { try portSelection.add() }
        portSelection.remove(portSelection.confirmed[0].id)
        expect(portSelection.confirmed.isEmpty, "A confirmed port mapping can be removed")
        let capacity = ResourceCapacity(cpuCount: 12, availableMemory: nil,
                                        totalMemory: 16 * bytesPerMemoryGB, totalDisk: nil)
        expect(capacity.cpuHint == "12 CPUs on your Mac", "Show Mac CPU count without VM limits")
        expect(capacity.memoryHint == "16 GB on your Mac" && capacity.availableMemoryHint == "Available memory unknown",
               "Separate total RAM from available RAM and preserve unknown availability")
        expect(capacity.diskHint == "Disk size unavailable", "Unavailable disk capacity must remain unknown")
        let copy = try BoxCommand.copy(source: "demo", name: "demo-copy", existing: ["demo"])
        expect(copy == ["copy", "demo", "demo-copy"], "Copy must target a distinct name")
        expectFailure("Copy onto source") { _ = try BoxCommand.copy(source: "demo", name: "demo", existing: []) }
        let resize = try BoxCommand.resize(box: boxes[1], cpus: "", memory: "0,5", disk: "")
        expect(resize == ["resize", "weekend-project", "--memory-gb", "0.5"], "Blank resize fields must not resend rounded resource values")
        let diskResize = try BoxCommand.resize(box: boxes[1], cpus: "4", memory: "", disk: "500")
        expect(diskResize == ["resize", "weekend-project", "--cpus", "4", "--disk-size-gb", "500"], "Resize flags")
        expectFailure("Running box resize") { _ = try BoxCommand.resize(box: boxes[0], cpus: "2", memory: "", disk: "") }
        expectFailure("Empty resize") { _ = try BoxCommand.resize(box: boxes[1], cpus: "", memory: "", disk: "") }
        expectFailure("Invalid decimal size") { _ = try BoxCommand.resize(box: boxes[1], cpus: "", memory: "NaN", disk: "") }
        let linuxCommand = "printf '%s' \"$HOME\" && echo `uname`"
        expect(BoxCommand.command(box: "demo", text: linuxCommand) == ["enter", "demo", "--", "/bin/bash", "-lc", linuxCommand], "Shell syntax must be passed as one argument to Linux")
        expect(BoxCommand.command(box: "demo", text: "   ") == nil, "Empty command")
        expect(BoxCommand.validExportPath("project with spaces"), "Relative export path")
        expect(BoxCommand.validExportPath("/workspace"), "Whole workspace export")
        expect(!BoxCommand.validExportPath("/etc/passwd"), "Export must remain in workspace")
        expect(!BoxCommand.validExportPath("../secret"), "Export traversal")
        let portList = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\n8080  8766  Apple   saved (fixed)\n3000  8767  MyBox   active\nForwarding stopped: test failure\nOutside ports bind to 127.0.0.1 on the Mac. Mappings use TCP.\n")
        expect(portList.ports.count == 2 && portList.ports[0].fixed && !portList.ports[1].fixed, "Fixed Apple mappings cannot offer removal")
        expect(portList.ports[1].state == "active" && portList.ports[1].mapping.outside == 8767, "Read actual forwarding status")
        expect(portList.warning == "Forwarding stopped: test failure", "Keep forwarding diagnostics")
        expect(portList.forwardingWarning(isRunning: false) == nil, "Stopped boxes must not show old forwarding failures")
        expect(portList.forwardingWarning(isRunning: true) == portList.warning, "Running boxes must still show forwarding failures")
        let noPorts = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\nNo port mappings.\nOutside ports bind to 127.0.0.1 on the Mac. Mappings use TCP.")
        expect(noPorts.ports.isEmpty, "Empty port list")
        let staleWithoutMappings = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\nNo port mappings.\nForwarding stopped: old failure\n")
        expect(staleWithoutMappings.forwardingWarning(isRunning: true) == nil, "Removed mappings must not show historical failures")
        expectFailure("Malformed port response") { _ = try PortList.parse("service failed") }
        expectFailure("Unknown port owner") { _ = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\n80 8080 unknown active") }
        expectFailure("Duplicate port row") { _ = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\n80 8080 MyBox active\n81 8080 MyBox active") }
        if CommandLine.arguments.count == 2 {
            let live = try WorkspaceParser.parse(String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8))
            print("Parsed CLI JSON: \(live.count) workspaces, \(live.filter(\.isRunning).count) running.")
        }
        print("Passed \(checks) checks: CLI parsing, command forms, port status, argument safety, errors, and timeout.")
    }
}
