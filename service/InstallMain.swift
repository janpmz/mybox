import Darwin
import Foundation

@main struct InstallMain {
    static func main() {
        do {
            guard CommandLine.arguments.count == 1 else { throw BoxError("Usage: mybox setup-service") }
            let directory = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().deletingLastPathComponent().path
            try installMyBoxService(artifactDirectory: directory)
        } catch { note("MyBox setup: \(error)"); exit(1) }
    }
}
