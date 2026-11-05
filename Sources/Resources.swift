import Foundation
import Darwin
import Virtualization

var containerStorageRoot: URL {
    #if MYBOX_TESTING
    if let path = ProcessInfo.processInfo.environment["MYBOX_TEST_CONTAINER_ROOT"] {
        return URL(fileURLWithPath: path, isDirectory: true)
    }
    #endif
    return files.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/com.apple.container/containers", isDirectory: true)
}

func resizeBytes(_ input: String, option: String, bytesPerGB: UInt64) throws -> UInt64 {
    let value = input.replacingOccurrences(of: Locale.current.decimalSeparator ?? ".", with: ".")
    guard value.utf8.count <= 32, matches(value, "^[0-9]+([.][0-9]+)?$"),
          let gb = Decimal(string: value, locale: Locale(identifier: "en_US_POSIX")), gb > 0 else {
        throw BoxError("Use a positive number such as 4 or 0.5 with \(option).")
    }
    // Apple configures memory in whole MiB. Use the same precision for both options.
    var mib = gb * Decimal(bytesPerGB) / Decimal(1 << 20)
    var rounded = Decimal()
    NSDecimalRound(&rounded, &mib, 0, .up)
    guard rounded <= Decimal(Int64.max >> 20) else { throw BoxError("\(option) is too large.") }
    return NSDecimalNumber(decimal: rounded).uint64Value << 20
}

struct ResizeOptions {
    static let usage = "Usage: mybox resize [name]\n       mybox resize <name> [--cpus N] [--memory-gb N] [--disk-size-gb N]"
    let name: String
    var cpus: Int?
    var memory: UInt64?
    var disk: UInt64?

    init(_ arguments: [String]) throws {
        guard let name = arguments.first else { throw BoxError(Self.usage) }
        _ = try containerID(name)
        self.name = name
        var seen = Set<String>()
        var index = 1
        while index < arguments.count {
            let option = arguments[index]
            guard ["--cpus", "--memory-gb", "--disk-size-gb"].contains(option) else {
                throw BoxError("Unknown resize option: \(option).\n\(Self.usage)")
            }
            guard seen.insert(option).inserted else { throw BoxError("\(option) was specified twice.") }
            guard index + 1 < arguments.count else { throw BoxError("Missing value for \(option).") }
            try set(option, to: arguments[index + 1])
            index += 2
        }
    }

    mutating func set(_ option: String, to value: String) throws {
        switch option {
        case "--cpus":
            guard matches(value, "^[1-9][0-9]*$"), let count = Int(value), count < Int.max else {
                throw BoxError("--cpus must be a positive whole number.")
            }
            cpus = count
        case "--memory-gb":
            let bytes = try resizeBytes(value, option: option, bytesPerGB: bytesPerMemoryGB)
            guard bytes >= 200 * (1 << 20) else { throw BoxError("Memory must be at least 200 MB.") }
            memory = bytes
        default:
            let bytes = try resizeBytes(value, option: option, bytesPerGB: bytesPerDiskGB)
            guard bytes >= 128 << 20, bytes < 16 << 40 else {
                throw BoxError("Disk size must be at least 128 MiB and less than 16 TiB.")
            }
            disk = bytes
        }
    }
}

private func estimatedAvailableMemory() -> UInt64? {
    let host = mach_host_self()
    defer { mach_port_deallocate(mach_task_self_, host) }
    var stats = vm_statistics64_data_t()
    let words = MemoryLayout.size(ofValue: stats) / MemoryLayout<integer_t>.stride
    var count = mach_msg_type_number_t(words)
    let result = withUnsafeMutablePointer(to: &stats) {
        $0.withMemoryRebound(to: integer_t.self, capacity: words) {
            host_statistics64(host, HOST_VM_INFO64, $0, &count)
        }
    }
    guard result == KERN_SUCCESS else { return nil }
    return (UInt64(stats.free_count) + UInt64(stats.inactive_count)) * UInt64(getpagesize())
}

private func capacityText(available: UInt64?, total: UInt64?, bytesPerGB: UInt64) -> String {
    let freeText = available.map { formatSize($0, scale: bytesPerGB, unit: "GB", decimals: 2) + " available" }
    let totalText = total.map { formatSize($0, scale: bytesPerGB, unit: "GB", decimals: 2) }
    if let freeText, let totalText { return "\(freeText) of \(totalText)" }
    if let totalText { return "\(totalText) total, free unknown" }
    return freeText ?? "free space unknown"
}

private func macDiskCapacity() -> String {
    let values = try? containerStorageRoot.resourceValues(forKeys: [.volumeAvailableCapacityKey, .volumeTotalCapacityKey])
    let reclaimable = try? containerStorageRoot.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
    let free = values?.volumeAvailableCapacity.flatMap(UInt64.init(exactly:))
    let available = reclaimable?.volumeAvailableCapacityForImportantUsage.flatMap(UInt64.init(exactly:))
    return capacityText(available: [free, available].compactMap { $0 }.max(),
                        total: values?.volumeTotalCapacity.flatMap(UInt64.init(exactly:)), bytesPerGB: bytesPerDiskGB)
}

private func resizeDefaultGB(_ bytes: UInt64, bytesPerGB: UInt64) -> String {
    // Round down by less than one allocation step so re-entering the value preserves a whole-MiB allocation.
    let gb = Decimal(bytes) / Decimal(bytesPerGB)
    let places = bytesPerGB == bytesPerMemoryGB ? 4 : 3
    return gb.formatted(.number.grouping(.never).rounded(rule: .down).precision(.fractionLength(0...places)))
}

private struct ResizeLimits {
    let maxCPUs: Int
    let memoryOverhead: UInt64 = 128 << 20
    let maxMemory: UInt64

    init(cpuOverhead: Int) throws {
        let vmCPUs = VZVirtualMachineConfiguration.maximumAllowedCPUCount
        guard (0..<vmCPUs).contains(cpuOverhead) else { throw BoxError("Invalid CPU overhead for this Mac.") }
        maxCPUs = vmCPUs - cpuOverhead
        let vmMemory = VZVirtualMachineConfiguration.maximumAllowedMemorySize
        guard vmMemory >= memoryOverhead else { throw BoxError("Cannot determine the memory limit for this VM.") }
        maxMemory = vmMemory - memoryOverhead
    }

    func validate(_ options: ResizeOptions) throws {
        if let cpus = options.cpus, cpus > maxCPUs {
            throw BoxError("This box supports at most --cpus \(maxCPUs) on this Mac.")
        }
        if let memory = options.memory, memory > maxMemory {
            let maxGB = Decimal((maxMemory >> 20) << 20) / Decimal(bytesPerMemoryGB)
            throw BoxError("This box supports at most --memory-gb \(maxGB) on this Mac.")
        }
    }
}

private func checkResizeCapacity(_ options: ResizeOptions, cpuOverhead: Int, showReport: Bool) throws {
    let host = ProcessInfo.processInfo
    let available = estimatedAvailableMemory()
    let limits = try ResizeLimits(cpuOverhead: cpuOverhead)
    if showReport {
        let ram = capacityText(available: available, total: host.physicalMemory, bytesPerGB: bytesPerMemoryGB)
        note("Mac: \(host.activeProcessorCount) CPUs, RAM \(ram), disk \(macDiskCapacity()).")
    }
    try limits.validate(options)
    if let cpus = options.cpus {
        let requested = cpus + cpuOverhead
        if requested > host.activeProcessorCount {
            note("Warning: This VM would have \(requested) virtual CPUs including overhead. They share \(host.activeProcessorCount) logical CPUs.")
        } else if Double(requested) >= Double(host.activeProcessorCount) * 0.8 {
            note("Warning: At full load, this VM could use at least 80% of the Mac's CPU capacity.")
        }
    }
    if let memory = options.memory {
        let vmBytes = memory + limits.memoryOverhead
        if let available, vmBytes > available {
            note("Warning: The VM needs \(memorySize(vmBytes)) including overhead. You currently have about \(memorySize(available)) of RAM available.")
        } else if Double(vmBytes) >= Double(host.physicalMemory) * 0.8 {
            note("Warning: The VM would use at least 80% of the Mac's RAM, including overhead.")
        }
    }
}

func regularResourceFile(_ url: URL) throws -> stat {
    var metadata = stat()
    guard lstat(url.path, &metadata) == 0,
          metadata.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
          metadata.st_uid == getuid(), metadata.st_nlink == 1 else {
        throw BoxError("Expected a regular file owned by you: \(url.path)")
    }
    return metadata
}

struct StoredBoxConfiguration {
    let url: URL
    let original: Data
    var document: [String: Any]
    let nested: Bool
    var configuration: [String: Any] {
        get { nested ? document["containerConfiguration"] as! [String: Any] : document }
        set {
            if nested { document["containerConfiguration"] = newValue }
            else { document = newValue }
        }
    }

    init(_ record: BoxRecord) throws {
        let directory = containerStorageRoot.appendingPathComponent(record.id, isDirectory: true)
        guard try itemKind(directory) == .directory else { throw BoxError("The box directory must not be a symlink.") }
        let config = directory.appendingPathComponent("config.json")
        nested = !exists(config)
        url = nested ? directory.appendingPathComponent("runtime-configuration.json") : config
        _ = try regularResourceFile(url)
        original = try Data(contentsOf: url)
        guard let object = try JSONSerialization.jsonObject(with: original) as? [String: Any],
              let saved = nested ? object["containerConfiguration"] as? [String: Any] : object,
              saved["id"] as? String == record.id,
              let created = saved["creationDate"] as? Double,
              let inspectedDate = record.configuration["creationDate"] as? String,
              let date = ISO8601DateFormatter().date(from: inspectedDate),
              abs(Date(timeIntervalSinceReferenceDate: created).timeIntervalSince(date)) < 1,
              (saved["labels"] as? [String: String])?[managedLabel] == "true",
              let resources = saved["resources"] as? [String: Any],
              let cpus = resources["cpus"] as? Int, cpus > 0,
              let memory = resources["memoryInBytes"] as? UInt64, memory > 0 else {
            throw BoxError("Cannot read the box's saved resource settings.")
        }
        // Apple caches resource values in its API. All other settings must still agree.
        var inspected = record.configuration
        var persisted = saved
        inspected.removeValue(forKey: "resources")
        persisted.removeValue(forKey: "resources")
        persisted["creationDate"] = inspected["creationDate"]
        guard NSDictionary(dictionary: inspected).isEqual(to: persisted) else {
            throw BoxError("The box configuration changed. Retry after Apple reloads its container service.")
        }
        document = object
    }

    func checkUnchanged() throws {
        guard try Data(contentsOf: url) == original else {
            throw BoxError("The box configuration changed during resize. Retry the command.")
        }
    }

    func save() throws {
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(to: url, options: .atomic)
    }
}

extension BoxRecord {
    var savedResources: [String: Any]? {
        // The API retains old limits until its next restart. Linux reads the saved configuration on each start.
        if isManaged, let stored = try? StoredBoxConfiguration(self) {
            return stored.configuration["resources"] as? [String: Any]
        }
        return configuration["resources"] as? [String: Any]
    }
}

func cloneResourceFile(_ source: URL, to destination: URL) throws {
    _ = try regularResourceFile(source)
    guard clonefile(source.path, destination.path, UInt32(CLONE_NOFOLLOW)) == 0 else {
        throw BoxError("Cannot clone the disk. Resize requires storage that supports APFS file clones: \(String(cString: strerror(errno)))")
    }
}

private func resizeAnswer(_ prompt: String) throws -> String {
    FileHandle.standardError.write(Data((prompt + " ").utf8))
    guard let line = readLine() else { throw BoxError("Resize canceled.") }
    return line.trimmingCharacters(in: .whitespaces)
}

extension Backend {
    private func chooseBoxForResize() throws -> String {
        let managed = try records(["list", "--all", "--format", "json"]).filter(\.isManaged)
        guard !managed.isEmpty else { throw BoxError("Create a box first with mybox create <name>.") }
        let names = managed.filter { $0.state == "stopped" }.map { String($0.id.dropFirst(namePrefix.count)) }.sorted()
        guard let first = names.first else { throw BoxError("Stop a box first with mybox stop <name>.") }
        if names.count == 1 { return first }
        for (index, name) in names.enumerated() { note("\(index + 1). \(name)") }
        while true {
            let answer = try resizeAnswer("Box number:")
            if let index = Int(answer), (1...names.count).contains(index) { return names[index - 1] }
            note("Enter a number from 1 to \(names.count).")
        }
    }

    func promptResize(name: String?) throws {
        let name = try name ?? chooseBoxForResize()
        let record = try managed(name)
        guard record.state == "stopped" else { throw BoxError("Stop the box first with mybox stop \(name).") }
        let stored = try StoredBoxConfiguration(record)
        let resources = stored.configuration["resources"] as! [String: Any]
        let limits = try ResizeLimits(cpuOverhead: resources["cpuOverhead"] as? Int ?? 1)
        let disk = containerStorageRoot.appendingPathComponent(record.id).appendingPathComponent("rootfs.ext4")
        let diskBytes = try exists(disk) ? UInt64(regularResourceFile(disk).st_size) : nil
        let memory = resizeDefaultGB(resources["memoryInBytes"] as! UInt64, bytesPerGB: bytesPerMemoryGB)
        note("Box: \(name)")
        if diskBytes == nil { note("Disk size becomes available after the first start.") }
        note("Press Enter to keep a value.")
        var options = try ResizeOptions([name])
        func ask(_ option: String, _ label: String, availability: () -> String) throws {
            while true {
                let answer = try resizeAnswer("\(label) (\(availability())):")
                if answer.isEmpty { break }
                do {
                    var candidate = options
                    try candidate.set(option, to: answer)
                    try limits.validate(candidate)
                    options = candidate
                    break
                } catch let error as BoxError { note(error.description) }
            }
        }
        try ask("--cpus", "CPUs [\(resources["cpus"] as! Int)]") {
            "\(ProcessInfo.processInfo.activeProcessorCount) CPUs available"
        }
        try ask("--memory-gb", "Memory in GB [\(memory)]") {
            capacityText(available: estimatedAvailableMemory(), total: ProcessInfo.processInfo.physicalMemory, bytesPerGB: bytesPerMemoryGB)
        }
        if let diskBytes {
            let disk = resizeDefaultGB(diskBytes, bytesPerGB: bytesPerDiskGB)
            try ask("--disk-size-gb", "Disk in GB [\(disk)]", availability: macDiskCapacity)
        }
        guard options.cpus != nil || options.memory != nil || options.disk != nil else {
            print("Kept the current settings for \(name).")
            return
        }
        try resize(options, expectedConfig: stored.original, expectedDiskBytes: diskBytes)
    }

    func resizeVolume(_ name: String, size: UInt64, created: inout [String]) throws -> URL {
        // Apple's formatter adds the journal to -s. Include it in the disk capacity the user requested.
        let journal = max(4 << 20, min(size / 64, size > 128 << 30 ? 1 << 30 : 128 << 20))
        _ = try run(["volume", "create", "--label", "\(helperLabel)=true", "--opt", "journal=ordered:\(journal)",
                     "-s", String(size - journal), name], capture: true)
        created.append(name)
        let data = try run(["volume", "inspect", name], capture: true)
        guard let items = try JSONSerialization.jsonObject(with: data) as? [[String: Any]], items.count == 1,
              items[0]["id"] as? String == name,
              let config = items[0]["configuration"] as? [String: Any],
              config["format"] as? String == "ext4", config["name"] as? String == name,
              let path = config["source"] as? String else { throw BoxError("Cannot read the temporary disk metadata.") }
        let expected = containerStorageRoot.deletingLastPathComponent().appendingPathComponent("volumes")
            .appendingPathComponent(name).appendingPathComponent("volume.img")
        guard URL(fileURLWithPath: path).standardizedFileURL == expected.standardizedFileURL else {
            throw BoxError("The temporary disk has an unexpected path.")
        }
        guard try regularResourceFile(expected).st_size == Int64(size) else {
            throw BoxError("Apple could not format a disk of exactly \(diskSize(size)). Try a different size.")
        }
        return expected
    }

    func prepareResizedDisk(_ disk: URL, size: UInt64, staged: URL) throws {
        let token = UUID().uuidString.lowercased()
        let sourceName = "mybox-resize-source-" + token
        let targetName = "mybox-resize-target-" + token
        let helper = "mybox-resize-helper-" + token
        var helperCreated = false
        var volumes: [String] = []
        defer {
            if helperCreated {
                _ = try? run(["stop", helper], capture: true)
                _ = try? run(["delete", helper], capture: true)
            }
            for name in volumes {
                do { _ = try run(["volume", "delete", name], capture: true) }
                catch { note("Could not remove temporary volume \(name). Remove it with: container volume delete \(name)") }
            }
        }
        let source = try resizeVolume(sourceName, size: 1 << 30, created: &volumes)
        let sourceClone = source.appendingPathExtension("clone")
        defer { try? files.removeItem(at: sourceClone) }
        try cloneResourceFile(disk, to: sourceClone)
        guard rename(sourceClone.path, source.path) == 0 else { throw BoxError("Cannot prepare the source disk clone.") }
        let target = try resizeVolume(targetName, size: size, created: &volumes)
        note("Copying the stopped filesystem into the new disk. This can take several minutes...")
        let arguments = ["create", "--name", helper, "--label", "\(helperLabel)=true", "--network", "none",
                         "--cpus", "1", "--memory", "256m", "--read-only",
                         "--mount", "type=volume,source=\(sourceName),target=/source,readonly",
                         "--mount", "type=volume,source=\(targetName),target=/target",
                         "--entrypoint", "/bin/sleep", trustedImage, "infinity"]
        _ = try run(arguments, capture: true)
        helperCreated = true
        _ = try run(["start", helper], capture: true)
        _ = try run(["exec", helper, "/bin/cp", "--archive", "--preserve=xattr", "--sparse=always", "/source/.", "/target/"], capture: true)
        _ = try run(["exec", helper, "/bin/sync", "-f", "/target"], capture: true)
        _ = try run(["stop", helper], capture: true)
        _ = try run(["delete", helper], capture: true)
        helperCreated = false
        try cloneResourceFile(target, to: staged)
    }

    func resize(_ options: ResizeOptions, expectedConfig: Data? = nil, expectedDiskBytes: UInt64? = nil) throws {
        let lock = try networkLock()
        defer { withExtendedLifetime(lock) {} }
        let record = try managed(options.name)
        guard record.state == "stopped" else {
            throw BoxError("Stop the box before resizing it: mybox stop \(options.name)")
        }
        var stored = try StoredBoxConfiguration(record)
        if let expectedConfig, expectedConfig != stored.original {
            throw BoxError("The box settings changed during input. Run mybox resize again.")
        }
        var config = stored.configuration
        let previous = config["resources"] as! [String: Any]
        try checkResizeCapacity(options, cpuOverhead: previous["cpuOverhead"] as? Int ?? 1, showReport: expectedConfig == nil)
        var resources = previous
        if let cpus = options.cpus { resources["cpus"] = cpus }
        if let memory = options.memory { resources["memoryInBytes"] = memory }
        let changeConfig = !NSDictionary(dictionary: previous).isEqual(to: resources)
        config["resources"] = resources
        stored.configuration = config
        let directory = containerStorageRoot.appendingPathComponent(record.id)
        let disk = directory.appendingPathComponent("rootfs.ext4")
        let staged = directory.appendingPathComponent(".mybox-resize-\(UUID().uuidString).ext4")
        defer { try? files.removeItem(at: staged) }
        var originalDisk: stat?
        var replaceDisk = false
        if let size = options.disk {
            guard exists(disk) else {
                throw BoxError("The box has no Linux disk yet. Start the box, stop it, then retry resize.")
            }
            let metadata = try regularResourceFile(disk)
            if let expectedDiskBytes, expectedDiskBytes != UInt64(metadata.st_size) {
                throw BoxError("The disk size changed during input. Run mybox resize again.")
            }
            originalDisk = metadata
            let rootfsURL = directory.appendingPathComponent("rootfs.json")
            _ = try regularResourceFile(rootfsURL)
            guard let rootfs = try JSONSerialization.jsonObject(with: Data(contentsOf: rootfsURL)) as? [String: Any],
                  rootfs["source"] as? String == disk.path,
                  let type = rootfs["type"] as? [String: Any],
                  let block = type["block"] as? [String: Any], block["format"] as? String == "ext4" else {
                throw BoxError("Disk resize requires the box's own ext4 root filesystem.")
            }
            if UInt64(metadata.st_size) != size {
                try prepareResizedDisk(disk, size: size, staged: staged)
                guard chmod(staged.path, metadata.st_mode & 0o777) == 0 else {
                    throw BoxError("Cannot preserve the Mac permissions on the Linux disk file.")
                }
                replaceDisk = true
            }
        }
        // Recheck the state before replacing files. MyBox holds its operation lock throughout the copy.
        let latest = try managed(options.name)
        guard latest.state == "stopped", latest.configuration["creationDate"] as? String == record.configuration["creationDate"] as? String else {
            throw BoxError("The box changed during resize. Its settings were not replaced.")
        }
        if let before = originalDisk {
            let after = try regularResourceFile(disk)
            guard before.st_dev == after.st_dev, before.st_ino == after.st_ino, before.st_size == after.st_size,
                  before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
                  before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec else {
                throw BoxError("The Linux disk changed during resize. Its settings were not replaced.")
            }
        }
        try stored.checkUnchanged()
        if changeConfig { try stored.save() }
        if replaceDisk, rename(staged.path, disk.path) != 0 {
            if changeConfig { try stored.original.write(to: stored.url, options: .atomic) }
            throw BoxError("Cannot replace the Linux disk. The original settings remain in place.")
        }
        print("Updated \(options.name).")
        print("Start it with: mybox start \(options.name)")
    }
}
