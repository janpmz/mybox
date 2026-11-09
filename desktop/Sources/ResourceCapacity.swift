import Darwin
import Foundation

/// Mac hardware totals and the same available-memory estimate used by CLI resize.
struct ResourceCapacity {
    let cpuCount: Int
    let availableMemory: UInt64?
    let totalMemory: UInt64
    let totalDisk: UInt64?

    static func current() -> ResourceCapacity {
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
        let availableMemory = result == KERN_SUCCESS
            ? (UInt64(stats.free_count) + UInt64(stats.inactive_count)) * UInt64(getpagesize()) : nil
        // MyBox's service stores its boxes on this volume. Do not substitute the
        // user's home volume, which may be a different disk.
        let storage = URL(fileURLWithPath: "/private/var/db/mybox-runtime")
        let disk = try? storage.resourceValues(forKeys: [.volumeTotalCapacityKey])
        return ResourceCapacity(cpuCount: ProcessInfo.processInfo.processorCount,
                                availableMemory: availableMemory, totalMemory: ProcessInfo.processInfo.physicalMemory,
                                totalDisk: disk?.volumeTotalCapacity.flatMap(UInt64.init(exactly:)))
    }

    var cpuHint: String { "\(cpuCount) CPUs on your Mac" }
    var memoryHint: String { "\(memorySize(totalMemory)) on your Mac" }
    var availableMemoryHint: String {
        availableMemory.map { "\(memorySize($0)) currently available" } ?? "Available memory unknown"
    }
    var diskHint: String {
        totalDisk.map { "\(formatSize($0, scale: bytesPerDiskGB, unit: "GB", decimals: 2)) disk on your Mac" }
            ?? "Disk size unavailable"
    }
}
