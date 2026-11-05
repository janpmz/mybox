import Foundation
import Darwin

struct FirewallSnapshot: Codable, Equatable {
    var interfaces: [HostInterface]
    var bridges: [String: String]
    var boot: String
    var blocked: Bool
    var routed: [String] = []
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
    for policy in store.pendingHostCleanup ?? [] { try policy.validate() }
    return store
}

func trackedInterfaces(_ interfaces: [HostInterface]) -> [HostInterface] {
    interfaces.filter { !$0.name.hasPrefix("vmenet") }.map {
        // Helper membership changes do not change the host destination policy.
        HostInterface(name: $0.name, ipv4: $0.ipv4.sorted(), networks4: $0.networks4.sorted(),
                      ipv6: $0.ipv6.sorted(), networks6: $0.networks6.sorted())
    }.sorted { $0.name < $1.name }
}

// A default route describes where internet packets go. VPNs can also install the
// two halves of a default, or routes covering the global IPv6 unicast space.
private let internetRoutes: Set<String> = [
    "0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1",
    "::/0", "::/1", "8000::/1", "::/2", "2000::/3", "2000::/4", "3000::/4",
]

func isTunnelInterface(_ name: String) -> Bool {
    ["utun", "ipsec", "ppp", "tun", "gif", "wg"].contains { name.hasPrefix($0) }
}

private func localNetworks(_ interface: HostInterface) -> [String] {
    let networks = interface.networks4 + interface.networks6
    return isTunnelInterface(interface.name) ? networks.filter { !internetRoutes.contains($0) } : networks
}

func blockedDestinations(_ interfaces: [HostInterface]) -> Set<String> {
    Set(interfaces.flatMap { item in
        item.ipv4.map { $0 + "/32" } + item.ipv6 + localNetworks(item)
    })
}

// Interface addresses omit prefixes routed behind a LAN router or through a VPN.
func routedDestinations(_ interfaces: [HostInterface]) throws -> [String] {
    // Tunnel interface masks can also be /0. Include their specific networks
    // explicitly instead of expanding (utunN:network) to the whole internet in PF.
    var destinations = Set(interfaces.filter { isTunnelInterface($0.name) }.flatMap(localNetworks))
    for family in ["inet", "inet6"] {
        let table = try hostCommand("/usr/sbin/netstat", ["-rn", "-f", family]).output
        guard table.contains("Destination"), table.contains("Netif") else {
            throw BoxError("Cannot identify destinations in the Mac routing table.")
        }
        for line in table.split(separator: "\n") {
            let row = line.split(whereSeparator: \.isWhitespace)
            guard row.count >= 4 else { continue }
            let (destination, flags, device) = (row[0], row[2], row[3])
            let tunnel = isTunnelInterface(String(device))
            guard flags.contains("U"), !flags.contains("W"), flags.contains("G") || tunnel else { continue }
            if destination == "default" { continue }
            let cidr = destination.split(separator: "/")
            guard let address = cidr.first?.split(separator: "%").first else { throw BoxError("Invalid routed address.") }
            let maxBits = family == "inet6" ? 128 : 32
            let length = cidr.count == 1 ? maxBits : Int(cidr[1])
            guard cidr.count <= 2, let length, (0...maxBits).contains(length) else {
                throw BoxError("Invalid routed prefix: \(destination)")
            }
            if length == 0 { continue }
            let network: String
            if family == "inet6" {
                // Multicast and link-local app traffic already hits the final IPv6 deny.
                if address.hasPrefix("ff") || address.hasPrefix("fe80:") { continue }
                network = try ipv6Network(String(address), length: length)
            } else {
                // Darwin abbreviates network addresses (for example, 10.25/16).
                let parts = address.split(separator: ".")
                guard (1...4).contains(parts.count), parts.allSatisfy({ UInt8($0) != nil }) else {
                    throw BoxError("Invalid IPv4 route: \(destination)")
                }
                let octets = parts.map(String.init) + Array(repeating: "0", count: 4 - parts.count)
                let bits = cidr.count == 1 && !flags.contains("H") ? parts.count * 8 : length
                network = octets.joined(separator: ".") + "/\(bits)"
            }
            if !tunnel || !internetRoutes.contains(network) {
                destinations.insert(network)
            }
        }
    }
    return destinations.sorted()
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
        let routed = live.isEmpty ? [] : try routedDestinations(interfaces)
        let snapshot = FirewallSnapshot(interfaces: trackedInterfaces(interfaces), bridges: bridges, boot: boot, blocked: false, routed: routed)
        let previous = (try? Data(contentsOf: firewallURL("snapshot.json"))).flatMap { try? JSONDecoder().decode(FirewallSnapshot.self, from: $0) }
        try checkPFParent()
        try enableFirewall(&store, boot: boot)
        let rules = try firewallRules(live, interfaces: interfaces, routed: routed)
        let savedRules = try? String(contentsOf: firewallURL("policy.pf"), encoding: .utf8)
        let expected = try? String(contentsOf: firewallURL("loaded.txt"), encoding: .utf8)
        let active = try pf(["-a", firewallAnchor, "-sr"])
        let changed = previous != snapshot || savedRules != rules || expected != active
        guard force || changed || starting != nil || !(store.pendingHostCleanup ?? []).isEmpty else { return false }

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
            let added = blockedDestinations(snapshot.interfaces).union(routed)
                .subtracting(blockedDestinations(previous!.interfaces).union(previous!.routed))
            for (key, bridge) in bridges.sorted(by: { $0.key < $1.key }) {
                if previous?.bridges[key] != bridge {
                    try clearBridgeStates([bridge])
                } else {
                    for destination in added.sorted() {
                        _ = try pf(["-i", bridge, "-k", destination.contains(":") ? "::/0" : "0.0.0.0/0", "-k", destination])
                    }
                }
            }
        }
        if let starting { try clearSubnetStates(starting) }
        for pending in store.pendingHostCleanup ?? [] {
            if live.contains(where: { $0.key == pending.key && $0.gateway == pending.gateway && $0.subnet4 == pending.subnet4 }) {
                _ = try pf(["-k", pending.gateway, "-k", pending.subnet4])
            }
        }
        if !(store.pendingHostCleanup ?? []).isEmpty {
            store.pendingHostCleanup = []
            try saveStore(store)
        }
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
