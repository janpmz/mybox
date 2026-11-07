import AppKit
import Foundation

@main
struct DesktopTests {
    static func main() throws {
        var checks = 0
        func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
            guard condition() else { fatalError(message) }
            checks += 1
        }
        func expectFailure(_ message: String, _ operation: () throws -> Void) {
            do { try operation(); fatalError(message) } catch { checks += 1 }
        }

        let table = """
        NAME              STATE    CORES  MEMORY   DISK USED  DISK MAX  PORTS
        demo              running  4      4 GB     2.8 GB     20 GB     inside 3000 / outside http://localhost:8766, inside 8080 / outside http://localhost:8080
        weekend-project   stopped  2      0.25 GB  -          -         -

        CORES and MEMORY are configured limits. DISK USED is the Linux disk's space on your Mac.
        DISK MAX is virtual capacity. Disk growth needs free space on your Mac.
        """
        let boxes = try WorkspaceParser.parse(table)
        expect(boxes.count == 2, "Expected both rows")
        expect(boxes[0].name == "demo" && boxes[0].isRunning, "Running state")
        expect(boxes[0].cores == "4" && boxes[0].memory == "4 GB", "Resource units must survive parsing")
        expect(boxes[0].diskUsed == "2.8 GB" && boxes[0].diskCapacity == "20 GB", "Disk columns")
        expect(boxes[0].ports == [PortMapping(inside: 3000, outside: 8766), PortMapping(inside: 8080, outside: 8080)], "Multiple current-format ports")
        expect(boxes[1].memory == "0.25 GB" && boxes[1].diskUsed == "-", "Fractional memory and unavailable disk")
        expect(!boxes[1].isRunning && boxes[1].ports.isEmpty, "Stopped state without ports")
        let legacy = try WorkspaceParser.parsePorts("inside 8080 / outside 8766 (localhost)")
        expect(legacy == [PortMapping(inside: 8080, outside: 8766)], "Installed older CLI port format")
        let empty = try WorkspaceParser.parse("No MyBox boxes yet. Create one with: mybox create demo\n")
        expect(empty.isEmpty, "Empty workspace list")
        expectFailure("Malformed output must not become an empty list") { _ = try WorkspaceParser.parse("permission denied") }
        expectFailure("Missing columns") { _ = try WorkspaceParser.parse("NAME  STATE\ndemo  running") }
        expectFailure("Truncated rows") { _ = try WorkspaceParser.parse("NAME  STATE  CORES  MEMORY  DISK USED  DISK MAX  PORTS\nbad") }
        expectFailure("Invalid port") { _ = try WorkspaceParser.parsePorts("inside 80 / outside http://localhost:70000") }
        expectFailure("Unexpected port format") { _ = try WorkspaceParser.parsePorts("inside 80 / outside https://example.com") }
        expectFailure("Duplicate workspaces") { _ = try WorkspaceParser.parse(table.replacingOccurrences(of: "weekend-project ", with: "demo            ")) }
        for name in ["", "Demo", "a;whoami", "../box", "a\n", "1box", String(repeating: "a", count: 49)] {
            expect(!WorkspaceParser.validName(name), "Invalid name accepted: \(name.debugDescription)")
        }
        expect(WorkspaceParser.validName("my-box-42"), "Valid box name")

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

        let create = try BoxCommand.create(name: "sample", cpus: "4", memory: "512m",
            ports: [PortDraft(outside: "8766", inside: "3000"), PortDraft(outside: "8767", inside: "8080")], existing: [])
        expect(create == ["create", "sample", "--cpus", "4", "--memory", "512m", "--inside-port", "3000", "--outside-port", "8766", "--inside-port", "8080", "--outside-port", "8767"], "Creation must keep port pairs in order and support memory in MB")
        expectFailure("Duplicate create port") {
            _ = try BoxCommand.create(name: "sample", cpus: "2", memory: "2g", ports: [PortDraft(outside: "80", inside: "80"), PortDraft(outside: "80", inside: "90")], existing: [])
        }
        expectFailure("Existing box name") { try BoxCommand.newName("demo", existing: ["demo"]) }
        expectFailure("Trailing hyphen") { try BoxCommand.newName("demo-", existing: []) }
        expectFailure("Invalid CPU") { _ = try BoxCommand.create(name: "sample", cpus: "1.5", memory: "2g", ports: [], existing: []) }
        expectFailure("Invalid memory units") { _ = try BoxCommand.create(name: "sample", cpus: "2", memory: "2GB", ports: [], existing: []) }
        expectFailure("Invalid port range") { _ = try PortDraft(outside: "0", inside: "3000").mapping() }
        expectFailure("Malformed numeric port") { _ = try PortDraft(outside: "8e3", inside: "3000").mapping() }
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
        let noPorts = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\nNo port mappings.\nOutside ports bind to 127.0.0.1 on the Mac. Mappings use TCP.")
        expect(noPorts.ports.isEmpty, "Empty port list")
        expectFailure("Malformed port response") { _ = try PortList.parse("service failed") }
        expectFailure("Unknown port owner") { _ = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\n80 8080 unknown active") }
        expectFailure("Duplicate port row") { _ = try PortList.parse("INSIDE  OUTSIDE  OWNER   STATE\n80 8080 MyBox active\n81 8080 MyBox active") }
        if CommandLine.arguments.count == 2 {
            let live = try WorkspaceParser.parse(String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8))
            print("Parsed live CLI output: \(live.count) workspaces, \(live.filter(\.isRunning).count) running.")
        }
        print("Passed \(checks) checks: CLI parsing, command forms, port status, argument safety, errors, and timeout.")
    }
}
