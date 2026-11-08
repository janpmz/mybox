import Darwin
import Foundation

typealias AccessCommand = (String, [String], Data, Double) throws -> ServiceCommandResult

func accessPrompt(_ args: [String]) throws -> String {
    guard args.count >= 2, matches(args[1], "^[a-z][a-z0-9-]{0,47}$"),
          !args.contains(where: { $0.utf8.contains(0) }) else { throw BoxError("Invalid box access request.") }
    func quoted(_ argument: String) throws -> String {
        let json = try JSONSerialization.data(withJSONObject: argument, options: [.fragmentsAllowed, .withoutEscapingSlashes])
        return String(decoding: json, as: UTF8.self).utf16.map { unit in
            unit < 128 ? String(UnicodeScalar(unit)!) : String(format: "\\u%04x", unit)
        }.joined()
    }
    let shown = try args.dropFirst(2).map(quoted).joined(separator: " ")
    switch (args[0], args.count) {
    case ("enter", 2): return "Open a shell in box “\(args[1])”. The requesting terminal controls this shell."
    case ("enter", _): return "Run in box “\(args[1])”. The requesting program controls input and receives output. Command: \(shown)"
    case ("export", 3): return "Give the requesting program a copy of \(shown) from box “\(args[1])”."
    case ("import", 3): return "Allow the requesting program to write \(shown) into box “\(args[1])”."
    default: throw BoxError("Unsupported box access request.")
    }
}

func nextAccessActions(_ sequence: [String]) -> [String]? {
    switch sequence {
    case []: return ["apply", "verify", "update-ports"]
    case ["apply"], ["apply", "verify"], ["update-ports"]: return ["verify"]
    default: return nil
    }
}

func prepareApprovedBox(_ connection: Int32, box: String,
                        command: AccessCommand = serviceCommand,
                        clock: () -> Double = { ProcessInfo.processInfo.systemUptime },
                        lifetime: Double = 300) throws {
    let deadline = clock() + lifetime
    let expires = Date().timeIntervalSince1970 + lifetime
    var actions: [String] = [], used = Set<String>(), policy: NSDictionary?
    while true {
        var remaining = deadline - clock()
        guard remaining > 0 else { throw BoxError("Box preparation approval expired.") }
        try socketTimeout(connection, remaining)
        let value: Any
        do { value = try receivePacket(connection) } catch is StreamClosed { return }
        remaining = deadline - clock()
        guard remaining > 0 else { throw BoxError("Box preparation approval expired.") }
        guard let message = value as? [String] else { throw BoxError("Unexpected approved firewall operation.") }
        if message == ["finish"] {
            guard [["verify"], ["apply", "verify", "verify"], ["update-ports", "verify"]].contains(actions) else { throw BoxError("Firewall preparation is incomplete.") }
            try sendPacket(connection, ["finished"]); return
        }
        guard message.count == 2, message[0] == "firewall", let allowed = nextAccessActions(actions) else {
            throw BoxError("Unexpected approved firewall operation.")
        }
        let token = message[1]
        guard UUID(uuidString: token)?.uuidString.lowercased() == token, used.insert(token).inserted else { throw BoxError("Invalid or reused firewall request.") }
        let scope: [String: Any] = ["box": box, "expires": expires, "actions": allowed, "policy": policy ?? NSNull()]
        let result: ServiceCommandResult
        do {
            result = try command(firewallProgram, ["approve-access", token], JSONSerialization.data(withJSONObject: scope), remaining)
        } catch is ServiceTimeout {
            try sendPacket(connection, ["firewall-error", token, "Firewall preparation timed out; entry was refused."]); return
        }
        if result.status != 0 {
            let raw = String(decoding: result.error, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let sanitized = String(raw.unicodeScalars.map { scalar -> String in
                if scalar == "\n" || scalar == "\t" || scalar == " " { return String(scalar) }
                switch scalar.properties.generalCategory {
                case .control, .format, .spaceSeparator, .lineSeparator, .paragraphSeparator, .surrogate, .unassigned: return "?"
                default: return String(scalar)
                }
            }.joined().prefix(2000))
            let detail = sanitized.isEmpty ? "The firewall helper exited with status \(result.status)." : sanitized
            note(detail)
            try sendPacket(connection, ["firewall-error", token, detail]); return
        }
        guard let confirmation = try JSONSerialization.jsonObject(with: result.output) as? [String: Any],
              let action = confirmation["action"] as? String, allowed.contains(action),
              let confirmedPolicy = confirmation["policy"] as? NSDictionary,
              confirmedPolicy["network"] as? String == "mybox-net-" + box,
              policy == nil || policy!.isEqual(confirmedPolicy) else { throw BoxError("The firewall helper returned an unexpected confirmation.") }
        policy = confirmedPolicy; actions.append(action)
        guard clock() < deadline else { throw BoxError("Box preparation approval expired.") }
        try sendPacket(connection, ["firewall", token])
    }
}

func serveAccess(_ connection: Int32, expectedPeer: uid_t, owner: uid_t, consoleOwner: uid_t,
                 command: AccessCommand = serviceCommand) throws {
    guard try peerUID(connection) == expectedPeer else { throw BoxError("Only the private MyBox service can request approval.") }
    try socketTimeout(connection, 5)
    guard let message = try receivePacket(connection) as? [Any], message.count == 3,
          message[0] as? String == "access-v2", let challenge = message[1] as? String,
          matches(challenge, "^[0-9a-f]{64}$"), let args = message[2] as? [String] else { throw BoxError("Invalid box access request.") }
    let reason = try accessPrompt(args) + " Also prepare and check this box’s Mac firewall protection."
    guard owner > 0, consoleOwner == owner else { throw BoxError("Sign in to the Mac account that installed MyBox to approve access.") }
    let result = try command("/bin/launchctl", ["asuser", String(owner), "/usr/bin/sudo", "-n", "-H", "-u", "#\(owner)", "--", helperDirectory + "/mybox-authenticate"],
                             JSONSerialization.data(withJSONObject: [challenge, reason]), 115)
    guard result.status == 0, result.output == Data(challenge.utf8) else { throw BoxError("Box access was not approved.") }
    try sendPacket(connection, ["approved", challenge])
    try prepareApprovedBox(connection, box: args[1], command: command)
}
