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
            print("mybox-firewall-manager 0.1.0")
            return
        }
        do {
            try firewallMain(arguments)
        } catch let error as CommandFailed {
            exit(error.status > 0 && error.status < 256 ? error.status : 1)
        } catch {
            note("mybox-firewall-manager: \(error)")
            exit(1)
        }
    }
}
