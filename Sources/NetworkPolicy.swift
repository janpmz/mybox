import Foundation
import Darwin

let firewallExecutable = "/Library/PrivilegedHelperTools/mybox-firewall-manager"
let firewallVersion = 3

final class FileLock {
    let descriptor: Int32
    init(_ path: String) throws {
        descriptor = open(path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw BoxError("Cannot open the MyBox lock.") }
        guard flock(descriptor, LOCK_EX) == 0 else { close(descriptor); throw BoxError("Cannot lock MyBox operations.") }
    }
    deinit { flock(descriptor, LOCK_UN); close(descriptor) }
}

struct HostInterface: Codable, Equatable {
    let name: String
    var ipv4: [String] = []
    var members: [String] = []
    var networks4: [String] = []
}

func ipv4Network(_ address: String, mask: String) throws -> String {
    let octets = address.split(separator: ".").compactMap { UInt8($0) }
    guard octets.count == 4, mask.hasPrefix("0x"), let bits = UInt32(mask.dropFirst(2), radix: 16),
          (~bits &+ 1) & ~bits == 0 else { throw BoxError("Cannot read an IPv4 interface address or netmask.") }
    let value = octets.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) } & bits
    return [24, 16, 8, 0].map { String((value >> $0) & 255) }.joined(separator: ".") + "/\(bits.nonzeroBitCount)"
}

func hostInterfaces() throws -> [HostInterface] {
    let text = try hostCommand("/sbin/ifconfig", []).output
    var result: [HostInterface] = []
    for line in text.split(separator: "\n") {
        if !line.hasPrefix(" ") && !line.hasPrefix("\t"), let colon = line.firstIndex(of: ":") {
            result.append(HostInterface(name: String(line[..<colon])))
        } else if !result.isEmpty {
            let words = line.split(whereSeparator: \.isWhitespace).map(String.init)
            if words.count > 1 && words[0] == "inet" {
                guard let mask = words.firstIndex(of: "netmask"), words.indices.contains(mask + 1) else {
                    throw BoxError("Cannot read an IPv4 interface netmask.")
                }
                result[result.count - 1].ipv4.append(words[1])
                result[result.count - 1].networks4.append(try ipv4Network(words[1], mask: words[mask + 1]))
            }
            if words.count > 1 && words[0] == "member:" { result[result.count - 1].members.append(words[1]) }
        }
    }
    guard !result.isEmpty else { throw BoxError("Cannot read Mac network interfaces.") }
    return result
}

struct NetworkPolicy: Codable, Equatable {
    var owner: UInt32
    var network: String
    var subnet4: String
    var subnet6: String
    var gateway: String
    var ports: [Int]
    var active = true
    var key: String { "\(owner)-\(network)" }

    func validate() throws {
        guard matches(network, "^mybox-net-[a-z][a-z0-9-]{0,47}$"),
              let slash = subnet4.firstIndex(of: "/"), subnet4[slash...] == "/24",
              ports.count <= 64, Set(ports).count == ports.count, ports.allSatisfy({ (1...65535).contains($0) }) else {
            throw BoxError("Invalid MyBox network policy.")
        }
        let octets = subnet4[..<slash].split(separator: ".").compactMap { UInt8($0) }
        guard octets.count == 4, octets[3] == 0,
              octets.map(String.init).joined(separator: ".") == String(subnet4[..<slash]),
              octets[0] == 10 || (octets[0] == 172 && (16...31).contains(octets[1])) || (octets[0] == 192 && octets[1] == 168),
              gateway == octets.prefix(3).map(String.init).joined(separator: ".") + ".1",
              matches(subnet6, "^[0-9a-f:]+/64$"), subnet6.hasPrefix("fd") || subnet6.hasPrefix("fc") else {
            throw BoxError("MyBox requires a private IPv4 /24 and IPv6 /64 network.")
        }
        var address = in6_addr()
        guard inet_pton(AF_INET6, String(subnet6.dropLast(3)), &address) == 1 else { throw BoxError("Invalid IPv6 subnet.") }
    }

    func bridge(in interfaces: [HostInterface]) throws -> String {
        let found = interfaces.filter { $0.ipv4.contains(gateway) }
        guard found.count == 1, let item = found.first,
              matches(item.name, "^bridge[0-9]+$"), !item.members.isEmpty,
              item.members.allSatisfy({ matches($0, "^vmenet[0-9]+$") }) else {
            throw BoxError("The dedicated network bridge is absent or cannot be identified safely.")
        }
        return item.name
    }
}
