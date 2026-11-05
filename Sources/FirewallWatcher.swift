import Foundation
import Darwin
import SystemConfiguration

struct FirewallWatcherStatus: Codable {
    var pid = getpid()
    var lastCheck = Date().timeIntervalSince1970
    var lastChange: Double?
    var refreshCount = 0
    var reason = "startup"
    var duration: Double = 0
    var error: String?
}

final class FirewallWatcher {
    let queue = DispatchQueue(label: "io.github.janpmz.my-container-box.firewall.watch")
    var pending = false
    var status = FirewallWatcherStatus()
    var previousError: String?
    var store: SCDynamicStore?
    var routeSource: DispatchSourceRead?
    var timer: DispatchSourceTimer?

    func request(_ reason: String) {
        // Bound the coalescing delay. Repeated events cannot postpone a refresh indefinitely.
        guard !pending else { return }
        pending = true
        queue.asyncAfter(deadline: .now() + .milliseconds(50)) {
            self.pending = false
            self.refresh(reason)
        }
    }

    func refresh(_ reason: String) {
        let start = Date()
        status.reason = reason
        do {
            let changed = try refreshFirewall()
            status.error = nil
            if changed {
                status.lastChange = Date().timeIntervalSince1970
                status.refreshCount += 1
                note("MyBox firewall refreshed after \(reason).")
            } else if previousError != nil {
                note("MyBox firewall protection recovered.")
            }
        } catch {
            status.error = String(describing: error)
            if status.error != previousError { note("MyBox firewall watcher: \(error)") }
        }
        previousError = status.error
        status.lastCheck = Date().timeIntervalSince1970
        status.duration = Date().timeIntervalSince(start)
        do {
            try JSONEncoder().encode(status).write(to: firewallURL("watcher.json"), options: .atomic)
        } catch { note("Cannot save the firewall watcher status: \(error)") }
    }

    func run() throws -> Never {
        try requireFirewallAdministrator()
        try rootDirectory(firewallDirectory)
        let lifetimeLock = try FileLock(firewallDirectory + "/watch.lock")
        var context = SCDynamicStoreContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
                                            retain: nil, release: nil, copyDescription: nil)
        store = SCDynamicStoreCreate(nil, "MyBox firewall watcher" as CFString, { _, _, info in
            guard let info else { return }
            Unmanaged<FirewallWatcher>.fromOpaque(info).takeUnretainedValue().request("network notification")
        }, &context)
        if let store {
            let patterns = ["State:/Network/Interface.*", "State:/Network/Service/.*/IPv[46]", "State:/Network/Global/IPv[46]"]
            if !SCDynamicStoreSetNotificationKeys(store, nil, patterns as CFArray) || !SCDynamicStoreSetDispatchQueue(store, queue) {
                note("Mac network notifications are unavailable. Route events and periodic checks remain enabled.")
            }
        }

        // Routing events also cover interfaces that have no configured network service.
        let descriptor = socket(PF_ROUTE, SOCK_RAW, AF_UNSPEC)
        if descriptor >= 0 && fcntl(descriptor, F_SETFL, O_NONBLOCK) == 0 && fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0 {
            let source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: queue)
            source.setEventHandler {
                var buffer = [UInt8](repeating: 0, count: 65536)
                // Limit each drain so continuous route traffic cannot starve the refresh.
                for _ in 0..<32 {
                    let count = read(descriptor, &buffer, buffer.count)
                    if count <= 0 { break }
                }
                self.request("route notification")
            }
            source.setCancelHandler { close(descriptor) }
            source.resume()
            routeSource = source
        } else {
            if descriptor >= 0 { close(descriptor) }
            note("Route notifications are unavailable. Periodic checks remain enabled.")
        }

        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .seconds(5), leeway: .milliseconds(100))
        timer.setEventHandler { self.request("periodic check") }
        timer.resume()
        self.timer = timer
        withExtendedLifetime(lifetimeLock) { dispatchMain() }
    }
}

func firewallLaunchConfiguration() -> [String: Any] {
    ["Label": "io.github.janpmz.my-container-box.firewall", "ProgramArguments": [firewallExecutable, "watch"],
     "RunAtLoad": true, "KeepAlive": true, "ThrottleInterval": 5,
     "StandardErrorPath": "/var/log/mybox-firewall.log"]
}
