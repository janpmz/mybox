import Foundation
import Darwin

@main
struct FirewallManagerMain {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments.isEmpty || ["help", "--help", "-h"].contains(arguments[0]) {
            print("mybox-firewall-manager: installs, watches, applies, checks, and removes MyBox firewall rules.")
            print("MyBox calls this program when firewall work is required.")
            return
        }
        if arguments == ["--version"] {
            print("mybox-firewall-manager 0.1.1")
            return
        }
        do {
            if arguments.first == "approve-access" {
                guard arguments.count == 2, getuid() == 0, geteuid() == 0 else {
                    throw BoxError("Only the administrator service can use an access approval.")
                }
                let input = FileHandle.standardInput.readData(ofLength: 8193)
                guard input.count <= 8192 else { throw BoxError("The access approval is too large.") }
                let scope = try JSONDecoder().decode(ServiceAccessScope.self, from: input)
                let confirmed = try approveServiceFirewall(arguments[1], accessScope: scope)
                try FileHandle.standardOutput.write(contentsOf: JSONEncoder().encode(confirmed))
            }
            else if arguments == ["verify-runtime-boot"] { try verifyRuntimeBoot() }
            else { try firewallMain(arguments) }
        } catch let error as CommandFailed {
            exit(error.status > 0 && error.status < 256 ? error.status : 1)
        } catch {
            note("mybox-firewall-manager: \(error)")
            exit(1)
        }
    }
}
