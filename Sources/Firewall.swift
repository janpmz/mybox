import Foundation
import Darwin

var firewallDirectory: String {
    #if MYBOX_TESTING
    if let path = ProcessInfo.processInfo.environment["MYBOX_TEST_FIREWALL_DIRECTORY"] { return path }
    #endif
    return "/var/db/mybox"
}
let firewallAnchor = "com.apple/000.mybox"

let privateIPv4 = "0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4"

func firewallRules(_ policies: [NetworkPolicy], interfaces: [HostInterface], routed: [String] = []) throws -> String {
    // Include dormant host interfaces so their later address changes are covered.
    // vmenet members are transient; their parent bridge is the filtering boundary.
    let names = interfaces.filter { !$0.name.hasPrefix("vmenet") }.map(\.name).sorted()
    guard !names.isEmpty, names.allSatisfy({ matches($0, "^[a-z][a-z0-9]{0,15}$") }) else { throw BoxError("Cannot identify Mac network interfaces.") }
    let host = "{ " + names.map { "(\($0))" }.joined(separator: ", ") + " }"
    let connected = "{ " + names.filter { !isTunnelInterface($0) }.map { "(\($0):network)" }.joined(separator: ", ") + " }"
    var rules = ["# MyBox owns only this anchor."]
    for policy in policies.sorted(by: { $0.key < $1.key }) where policy.active {
        try policy.validate()
        let bridge = try policy.bridge(in: interfaces)
        // No incoming rule selects packets by a guest-supplied source address.
        // vmnet advertises the default gateway. Allow discovery on this link without
        // allowing a guest to advertise itself as a router or open a host service.
        let neighbors = "{ ::, fe80::/10, \(policy.subnet6) }"
        rules += [
            "pass in quick on \(bridge) inet6 proto icmp6 from \(neighbors) to ff02::2 icmp6-type 133 no state",
            "pass in quick on \(bridge) inet6 proto icmp6 from \(neighbors) to { fe80::/10, (\(bridge)), ff02::1:ff00:0/104, ff02::1 } icmp6-type { 135, 136 } no state",
            "pass out quick on \(bridge) inet6 proto icmp6 from fe80::/10 to { fe80::/10, \(policy.subnet6), ff02::/16 } icmp6-type 134 no state",
            "pass out quick on \(bridge) inet6 proto icmp6 from { fe80::/10, (\(bridge)) } to { fe80::/10, \(policy.subnet6), ff02::/16 } icmp6-type { 135, 136 } no state",
        ]
        if !routed.isEmpty {
            rules.append("block drop in quick on \(bridge) from any to { \(routed.joined(separator: ", ")) } label \"mybox_routed\"")
        }
        rules += [
            "block drop in quick on \(bridge) inet from any to \(host) label \"mybox_host\"",
            "block drop in quick on \(bridge) inet from any to \(connected) label \"mybox_lan\"",
            "block drop in quick on \(bridge) inet from any to { \(privateIPv4) } label \"mybox_private\"",
            "block drop in quick on \(bridge) inet6 from any to \(host) label \"mybox_host6\"",
            "block drop in quick on \(bridge) inet6 from any to \(connected) label \"mybox_lan6\"",
            "block drop in quick on \(bridge) inet6 from any to { 2001::/32, 2002::/16 } label \"mybox_transition6\"",
            "pass in quick on \(bridge) inet6 from \(policy.subnet6) to 2000::/3 keep state (if-bound) label \"mybox_internet6\"",
            "block drop in quick on \(bridge) inet6 all label \"mybox_other6\"",
            "block drop out quick on \(bridge) inet6 all label \"mybox_unpublished6\"",
        ]
        for port in policy.ports.sorted() {
            rules.append("pass out quick inet proto tcp from \(policy.gateway) to \(policy.subnet4) port \(port) flags S/SA keep state label \"mybox_selected\"")
        }
        rules += [
            "block drop out quick inet from any to \(policy.subnet4) label \"mybox_unpublished\"",
            "pass in quick on \(bridge) inet from any to any keep state (if-bound) label \"mybox_internet\"",
        ]
    }
    return rules.joined(separator: "\n") + "\n"
}

struct FirewallStore: Codable {
    // The acknowledgement protocol changes independently of saved policy data.
    var version = 1
    var policies: [NetworkPolicy] = []
    var boot = ""
    var token = ""
    var pendingHostCleanup: [NetworkPolicy]?
}

func rootDirectory(_ path: String, permissions: Int = 0o700) throws {
    if !files.fileExists(atPath: path) {
        try files.createDirectory(atPath: path, withIntermediateDirectories: false, attributes: [.posixPermissions: permissions])
    }
    var info = stat()
    var owner: uid_t = 0
    #if MYBOX_TESTING
    if ProcessInfo.processInfo.environment["MYBOX_TEST_FIREWALL_DIRECTORY"] != nil { owner = getuid() }
    #endif
    guard lstat(path, &info) == 0, info.st_uid == owner, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
          info.st_mode & 0o022 == 0 else { throw BoxError("Unsafe firewall directory: \(path)") }
}

func pf(_ arguments: [String], check: Bool = true) throws -> String {
    try hostCommand("/sbin/pfctl", arguments, check: check).output
}

func validatePFParentRules(_ output: String) throws {
    let lines = output.split(separator: "\n").map { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ") }
        .filter { !$0.hasPrefix("No ALTQ") && !$0.hasPrefix("ALTQ") && !$0.isEmpty }
    let scrubRules: Set<String> = [
        "scrub-anchor \"com.apple/*\" all fragment reassemble",
        "scrub-anchor \"com.apple.internet-sharing\" all fragment reassemble",
    ]
    let filterRules = lines.filter { !scrubRules.contains($0) }
    let apple = "anchor \"com.apple/*\" all"
    let sharing = "anchor \"com.apple.internet-sharing\" all"
    // Apple's normalization entries do not bypass filtering. Internet Sharing
    // may follow the Apple wildcard; MyBox's quick rules run before it.
    guard filterRules == [apple] || filterRules == [apple, sharing] else {
        throw BoxError("The active main PF rules differ from the supported macOS rules. MyBox did not replace them.")
    }
}

func checkPFParent() throws {
    try validatePFParentRules(pf(["-sr"]))
    let anchors = try pf(["-a", "com.apple", "-s", "Anchors"]).split(separator: "\n")
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty && !$0.hasPrefix("No ALTQ") && !$0.hasPrefix("ALTQ") }
    guard anchors.allSatisfy({ ($0.split(separator: "/").last.map(String.init) ?? $0) >= "000.mybox" }) else {
        throw BoxError("Another PF anchor runs before MyBox. The operation was refused.")
    }
}

func checkPFEnabled() throws {
    guard try pf(["-si"]).contains("Status: Enabled") else { throw BoxError("PF is disabled. The box will not start.") }
}

func saveStore(_ store: FirewallStore) throws {
    try JSONEncoder().encode(store).write(to: URL(fileURLWithPath: firewallDirectory + "/state.json"), options: .atomic)
}

func firewallMain(_ arguments: [String], serviceOwner: UInt32? = nil, checkPending: (() throws -> Void)? = nil) throws {
    if arguments.first == "approve" {
        guard arguments.count == 2 else { throw BoxError("Expected a firewall request number.") }
        try approveServiceFirewall(arguments[1])
        return
    }
    guard let action = arguments.first else { throw BoxError("Missing firewall operation.") }
    // Pure rule rendering is available for offline tests. It changes no PF state.
    if action == "render", arguments.count == 3 {
        let policy = try JSONDecoder().decode(NetworkPolicy.self, from: Data(arguments[1].utf8))
        let names = arguments[2].split(separator: ",").map(String.init)
        guard names.count >= 2 else { throw BoxError("Missing test interfaces.") }
        let interfaces = [HostInterface(name: names[0], ipv4: [policy.gateway], members: ["vmenet999"])]
            + names.dropFirst().map { HostInterface(name: $0, ipv4: ["127.0.0.1"]) }
        print(try firewallRules([policy], interfaces: interfaces), terminator: "")
        return
    }
    if action == "watch", arguments.count == 1 { try FirewallWatcher().run() }
    #if MYBOX_TESTING
    if action == "_test-launch-config", arguments.count == 1 {
        let data = try JSONSerialization.data(withJSONObject: firewallLaunchConfiguration(), options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
        return
    }
    #endif
    guard ["install", "apply", "update-ports", "verify", "remove", "restore", "refresh", "status"].contains(action) else {
        throw BoxError("Unknown firewall operation: \(action).")
    }
    let policyAction = ["apply", "update-ports", "verify", "remove"].contains(action)
    guard arguments.count == (policyAction ? 2 : 1) else { throw BoxError("Invalid firewall arguments.") }
    try requireFirewallAdministrator()
    try rootDirectory(firewallDirectory)
    let lock = try FileLock(firewallDirectory + "/lock")
    defer { withExtendedLifetime(lock) {} }
    try checkPending?()
    if action == "status" {
        let data = try Data(contentsOf: firewallURL("watcher.json"))
        print(String(decoding: data, as: UTF8.self))
        return
    }
    if action == "install" {
        try checkPFParent()
        let parent = (firewallExecutable as NSString).deletingLastPathComponent
        try rootDirectory(parent, permissions: 0o755)
        let current = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let data = try Data(contentsOf: current)
        try data.write(to: URL(fileURLWithPath: firewallExecutable), options: .atomic)
        guard chown(firewallExecutable, 0, 0) == 0, chmod(firewallExecutable, 0o755) == 0 else {
            throw BoxError("Cannot secure the installed firewall helper.")
        }
        let path = "/Library/LaunchDaemons/io.github.janpmz.my-container-box.firewall.plist"
        try PropertyListSerialization.data(fromPropertyList: firewallLaunchConfiguration(), format: .xml, options: 0)
            .write(to: URL(fileURLWithPath: path), options: .atomic)
        guard chown(path, 0, 0) == 0, chmod(path, 0o644) == 0 else { throw BoxError("Cannot secure the startup configuration.") }
    }
    var store = try readFirewallStore()
    var requested: NetworkPolicy?
    if policyAction {
        var policy = try JSONDecoder().decode(NetworkPolicy.self, from: Data(arguments[1].utf8))
        try policy.validate()
        guard let caller = serviceOwner ?? ProcessInfo.processInfo.environment["SUDO_UID"].flatMap(UInt32.init), caller == policy.owner else {
            throw BoxError("The network policy belongs to another Mac user.")
        }
        if action != "remove" { _ = try policy.bridge(in: hostInterfaces()) }
        requested = policy
        if action == "update-ports" {
            guard let previous = store.policies.first(where: { $0.key == policy.key }), previous.active else {
                throw BoxError("The box needs an active firewall policy before a live port change.")
            }
            var expected = previous
            expected.ports = policy.ports
            guard policy == expected else { throw BoxError("A live port change cannot change the box's network identity.") }
            if !Set(previous.ports).subtracting(policy.ports).isEmpty {
                // macOS pfctl supports source/destination state removal, but no
                // port selector. Close Mac-to-box states, preserving outbound
                // box connections such as Codex sessions.
                store.pendingHostCleanup = (store.pendingHostCleanup ?? []).filter { $0.key != previous.key } + [previous]
            }
        }
        if action == "verify" {
            guard store.policies.contains(policy) else { throw BoxError("This box has no matching active firewall policy.") }
        } else {
            policy.active = action != "remove"
            store.policies.removeAll { $0.key == policy.key }
            store.policies.append(policy)
            // Save the intended policy before applying it so the watcher can retry a failed refresh.
            try saveStore(store)
        }
    }
    _ = try reconcileFirewall(&store, force: ["install", "apply", "remove", "restore"].contains(action),
                              starting: action == "apply" ? requested : nil)
    if action == "install" {
        // Update the existing startup job to use the dedicated manager.
        let job = "system/io.github.janpmz.my-container-box.firewall"
        if try hostCommand("/bin/launchctl", ["print", job], check: false).status == 0 {
            _ = try hostCommand("/bin/launchctl", ["bootout", job])
        }
        let watcherStatus = firewallURL("watcher.json")
        if files.fileExists(atPath: watcherStatus.path) { try files.removeItem(at: watcherStatus) }
        _ = try hostCommand("/bin/launchctl", ["bootstrap", "system", "/Library/LaunchDaemons/io.github.janpmz.my-container-box.firewall.plist"])
        let legacy = "/Library/PrivilegedHelperTools/com.mybox.firewall"
        var info = stat()
        if lstat(legacy, &info) == 0 {
            guard info.st_uid == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG) else {
                throw BoxError("The old MyBox executable has unexpected ownership or type. It was left in place.")
            }
            try files.removeItem(atPath: legacy)
        }
    }
    print("mybox-firewall-manager-\(firewallVersion)-ok")
}
