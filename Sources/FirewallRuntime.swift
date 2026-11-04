import Foundation
import Darwin

struct FirewallSnapshot: Codable, Equatable {
    var interfaces: [HostInterface]
    var bridges: [String: String]
    var boot: String
    var blocked: Bool
}

func requireFirewallAdministrator() throws {
    #if MYBOX_TESTING
    if ProcessInfo.processInfo.environment["MYBOX_TEST_FIREWALL_DIRECTORY"] != nil { return }
    #endif
    guard geteuid() == 0 else { throw BoxError("The firewall operation requires macOS administrator authentication.") }
}

func firewallURL(_ name: String) -> URL {
    URL(fileURLWithPath: firewallDirectory).appendingPathComponent(name)
}

func readFirewallStore() throws -> FirewallStore {
    let url = firewallURL("state.json")
    let store = files.fileExists(atPath: url.path) ? try JSONDecoder().decode(FirewallStore.self, from: Data(contentsOf: url)) : FirewallStore()
    guard store.version == 1 else { throw BoxError("Unsupported saved firewall policy version.") }
    for policy in store.policies { try policy.validate() }
    return store
}

func trackedInterfaces(_ interfaces: [HostInterface]) -> [HostInterface] {
    interfaces.filter { !$0.name.hasPrefix("vmenet") }.map {
        // Helper membership changes do not change the host destination policy.
        HostInterface(name: $0.name, ipv4: $0.ipv4.sorted(), networks4: $0.networks4.sorted())
    }.sorted { $0.name < $1.name }
}

func blockedDestinations(_ interfaces: [HostInterface]) -> Set<String> {
    Set(interfaces.flatMap { $0.ipv4.map { $0 + "/32" } + $0.networks4 })
}

func liveFirewallPolicies(_ store: FirewallStore, interfaces: [HostInterface]) throws -> [NetworkPolicy] {
    try store.policies.filter { policy in
        guard policy.active, interfaces.contains(where: { $0.ipv4.contains(policy.gateway) }) else { return false }
        _ = try policy.bridge(in: interfaces)
        return true
    }
}

func enableFirewall(_ store: inout FirewallStore, boot: String) throws {
    let references = try pf(["-s", "References"])
    if store.boot != boot || store.token.isEmpty || !references.contains(store.token) {
        let output = try pf(["-E"])
        guard let range = output.range(of: "Token\\s*:\\s*[0-9]+", options: .regularExpression),
              let token = output[range].split(whereSeparator: \.isWhitespace).last else {
            throw BoxError("PF did not return an enable reference.")
        }
        store.boot = boot
        store.token = String(token)
        try saveStore(store)
    }
    try checkPFEnabled()
}

func replaceFirewallRules(_ rules: String) throws {
    let pending = firewallURL("pending.pf")
    try Data(rules.utf8).write(to: pending, options: .atomic)
    _ = try pf(["-a", firewallAnchor, "-nf", pending.path])
    // PF replaces this anchor in one transaction. Never flush it before loading.
    _ = try pf(["-a", firewallAnchor, "-f", pending.path])
    let active = try pf(["-a", firewallAnchor, "-sr"])
    if rules.contains("block drop"), !active.contains("block drop") {
        throw BoxError("PF did not retain the MyBox blocking rules.")
    }
    try Data(rules.utf8).write(to: firewallURL("policy.pf"), options: .atomic)
    try Data(active.utf8).write(to: firewallURL("loaded.txt"), options: .atomic)
}

func clearBridgeStates(_ bridges: [String]) throws {
    for bridge in Set(bridges).sorted() {
        // Outgoing rules bind states to the bridge, including spoofed sources.
        for any in ["0.0.0.0/0", "::/0"] { _ = try pf(["-i", bridge, "-k", any]) }
    }
}

func clearSubnetStates(_ policy: NetworkPolicy) throws {
    // This also removes floating states for published incoming connections.
    for (subnet, any) in [(policy.subnet4, "0.0.0.0/0"), (policy.subnet6, "::/0")] {
        _ = try pf(["-k", subnet])
        _ = try pf(["-k", any, "-k", subnet])
    }
}

func suspendedFirewallRules(_ policies: [NetworkPolicy], interfaces: [HostInterface]) throws -> String {
    var rules = ["# MyBox networking is blocked while firewall recovery is pending."]
    for policy in policies.sorted(by: { $0.key < $1.key }) {
        let bridge = try policy.bridge(in: interfaces)
        rules += ["block drop quick on \(bridge) all label \"mybox_suspended\"",
                  "block drop out quick inet to \(policy.subnet4) label \"mybox_suspended\"",
                  "block drop out quick inet6 to \(policy.subnet6) label \"mybox_suspended\""]
    }
    return rules.joined(separator: "\n") + "\n"
}

func suspendFirewall(_ store: inout FirewallStore, interfaces: [HostInterface], boot: String) throws {
    // Recheck current identities. Never reuse a saved bridge number for a different network.
    let live = try liveFirewallPolicies(store, interfaces: interfaces)
    try checkPFParent()
    try enableFirewall(&store, boot: boot)
    try replaceFirewallRules(suspendedFirewallRules(live, interfaces: interfaces))
    let bridges = try Dictionary(uniqueKeysWithValues: live.map { ($0.key, try $0.bridge(in: interfaces)) })
    try clearBridgeStates(Array(bridges.values))
    for policy in live { try clearSubnetStates(policy) }
    let snapshot = FirewallSnapshot(interfaces: trackedInterfaces(interfaces), bridges: bridges, boot: boot, blocked: true)
    try JSONEncoder().encode(snapshot).write(to: firewallURL("snapshot.json"), options: .atomic)
}

@discardableResult
func reconcileFirewall(_ store: inout FirewallStore, force: Bool = false, starting: NetworkPolicy? = nil) throws -> Bool {
    var boot = ""
    do {
        let interfaces = try hostInterfaces()
        boot = try hostCommand("/usr/sbin/sysctl", ["-n", "kern.boottime"]).output
        let live = try liveFirewallPolicies(store, interfaces: interfaces)
        let bridges = try Dictionary(uniqueKeysWithValues: live.map { ($0.key, try $0.bridge(in: interfaces)) })
        let snapshot = FirewallSnapshot(interfaces: trackedInterfaces(interfaces), bridges: bridges, boot: boot, blocked: false)
        let previous = (try? Data(contentsOf: firewallURL("snapshot.json"))).flatMap { try? JSONDecoder().decode(FirewallSnapshot.self, from: $0) }
        try checkPFParent()
        try enableFirewall(&store, boot: boot)
        let rules = try firewallRules(live, interfaces: interfaces)
        let savedRules = try? String(contentsOf: firewallURL("policy.pf"), encoding: .utf8)
        let expected = try? String(contentsOf: firewallURL("loaded.txt"), encoding: .utf8)
        let active = try pf(["-a", firewallAnchor, "-sr"])
        let changed = previous != snapshot || savedRules != rules || expected != active
        guard force || changed || starting != nil else { return false }

        let wasBlocked = previous?.blocked == true || savedRules?.contains("mybox_suspended") == true
        if wasBlocked {
            // Keep the block in place until recovery has removed all old connection states.
            try clearBridgeStates(Array(bridges.values))
            for policy in live { try clearSubnetStates(policy) }
        }
        try replaceFirewallRules(rules)
        if !wasBlocked && (previous?.boot != boot || previous?.blocked != false || expected != active) {
            try clearBridgeStates(Array(bridges.values))
        } else if !wasBlocked {
            let added = blockedDestinations(snapshot.interfaces).subtracting(blockedDestinations(previous!.interfaces))
            for (key, bridge) in bridges.sorted(by: { $0.key < $1.key }) {
                if previous?.bridges[key] != bridge {
                    try clearBridgeStates([bridge])
                } else {
                    for destination in added.sorted() {
                        _ = try pf(["-i", bridge, "-k", "0.0.0.0/0", "-k", destination])
                    }
                }
            }
        }
        if let starting { try clearSubnetStates(starting) }
        try JSONEncoder().encode(snapshot).write(to: firewallURL("snapshot.json"), options: .atomic)
        return changed
    } catch {
        let original = error
        // A second fresh read handles a transient enumeration failure and avoids stale bridge identities.
        do {
            let current = try hostInterfaces()
            if boot.isEmpty { boot = try hostCommand("/usr/sbin/sysctl", ["-n", "kern.boottime"]).output }
            try suspendFirewall(&store, interfaces: current, boot: boot)
        } catch {
            note("CRITICAL: MyBox could not confirm a network block: \(error)")
        }
        throw BoxError("Firewall refresh failed. MyBox attempted to block box networking and will retry automatically. \(original)")
    }
}

@discardableResult
func refreshFirewall() throws -> Bool {
    try requireFirewallAdministrator()
    try rootDirectory(firewallDirectory)
    let lock = try FileLock(firewallDirectory + "/lock")
    defer { withExtendedLifetime(lock) {} }
    var store = try readFirewallStore()
    return try reconcileFirewall(&store)
}
