import Darwin
import Foundation

@main struct AccessMain {
    static func main() {
        do {
            guard Array(CommandLine.arguments.dropFirst()) == ["serve"], getuid() == 0, geteuid() == 0 else {
                throw BoxError("Only the administrator service can handle box approval.")
            }
            let peer = try serviceUID()
            guard let owner = UInt32(try String(contentsOfFile: helperDirectory + "/owner", encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)) else { throw BoxError("Invalid installation owner.") }
            var console = stat()
            guard stat("/dev/console", &console) == 0 else { throw systemError("Cannot identify the signed-in account") }
            try configureSocket(STDIN_FILENO)
            do { try serveAccess(STDIN_FILENO, expectedPeer: peer, owner: owner, consoleOwner: console.st_uid) }
            catch { try? sendPacket(STDIN_FILENO, ["denied"]) }
        } catch { note("MyBox: \(error)"); exit(1) }
    }
}
