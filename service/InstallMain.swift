import Darwin
import Foundation

@main struct InstallMain {
    static func main() {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            let directory = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().deletingLastPathComponent().path
            if arguments.isEmpty { try installMyBoxService(artifactDirectory: directory) }
            else if arguments.count == 2, arguments[0] == "--package-preflight" {
                try preparePackageInstallation(target: arguments[1])
            } else if arguments.count == 2, arguments[0] == "--package-install" {
                try finishPackageInstallation(target: arguments[1], artifactDirectory: directory)
            } else { throw BoxError("Usage: mybox setup-service") }
        } catch { note("MyBox setup: \(error)"); exit(1) }
    }
}
