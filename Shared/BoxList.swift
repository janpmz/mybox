import Foundation

/// The machine-readable contract shared by the CLI and optional desktop app.
struct BoxList: Codable, Sendable {
    static let currentSchemaVersion = 1
    let schemaVersion: Int
    let boxes: [BoxListEntry]
}

struct BoxListEntry: Codable, Sendable {
    struct Port: Codable, Sendable {
        let inside: Int
        let outside: Int
    }

    let name: String
    let state: String
    let cpus: Int?
    let memoryBytes: UInt64?
    let diskUsedBytes: UInt64?
    let diskCapacityBytes: UInt64?
    let ports: [Port]

    enum CodingKeys: String, CodingKey {
        case name, state, cpus, memoryBytes, diskUsedBytes, diskCapacityBytes, ports
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(name, forKey: .name)
        try values.encode(state, forKey: .state)
        // Unknown resources are explicit nulls, never guessed defaults or strings.
        try values.encode(cpus, forKey: .cpus)
        try values.encode(memoryBytes, forKey: .memoryBytes)
        try values.encode(diskUsedBytes, forKey: .diskUsedBytes)
        try values.encode(diskCapacityBytes, forKey: .diskCapacityBytes)
        try values.encode(ports, forKey: .ports)
    }
}

// macOS labels binary memory sizes and decimal disk sizes in GB.
let bytesPerMemoryGB: UInt64 = 1 << 30
let bytesPerDiskGB: UInt64 = 1_000_000_000

func formatSize(_ bytes: UInt64, scale: UInt64, unit: String, decimals: Int = 1) -> String {
    let value = (Double(bytes) / Double(scale)).formatted(
        .number.grouping(.never).precision(.fractionLength(0...decimals)))
    return "\(value) \(unit)"
}

func memorySize(_ bytes: UInt64) -> String {
    formatSize(bytes, scale: bytesPerMemoryGB, unit: "GB", decimals: 2)
}

func diskSize(_ bytes: UInt64) -> String {
    let units: [(UInt64, String)] = [(1000 * bytesPerDiskGB, "TB"), (bytesPerDiskGB, "GB"), (1_000_000, "MB"), (1000, "kB")]
    for (scale, unit) in units where bytes >= scale {
        return formatSize(bytes, scale: scale, unit: unit)
    }
    return "\(bytes) B"
}
