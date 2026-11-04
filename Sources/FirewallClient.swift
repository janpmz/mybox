import Foundation
import Darwin

func installedFirewall() throws {
    #if MYBOX_TESTING
    if ProcessInfo.processInfo.environment["MYBOX_TEST_HOST"] != nil { return }
    #endif
    var info = stat()
    guard lstat(firewallExecutable, &info) == 0, info.st_uid == 0,
          info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_mode & 0o022 == 0 else {
        throw BoxError("Run mybox setup-network once in a Mac terminal before starting a box.")
    }
}

func firewallRequest(_ action: String, policy: NetworkPolicy) throws {
    try installedFirewall()
    let json = String(decoding: try JSONEncoder().encode(policy), as: UTF8.self)
    let result = try hostCommand("/usr/bin/sudo", [firewallExecutable, action, json], terminal: true)
    guard result.output.trimmingCharacters(in: .whitespacesAndNewlines) == "mybox-firewall-manager-\(firewallVersion)-ok" else {
        throw BoxError("The firewall helper did not confirm protection. Run mybox setup-network to update it.")
    }
}
