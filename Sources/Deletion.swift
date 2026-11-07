import Foundation

struct DeleteOptions {
    let name: String
    let confirmed: Bool

    init(_ arguments: [String]) throws {
        guard let name = arguments.first,
              arguments.count == 1 || (arguments.count == 2 && arguments[1] == "--yes") else {
            throw BoxError("Usage: mybox delete <name> [--yes]")
        }
        _ = try containerID(name)
        self.name = name
        confirmed = arguments.count == 2
    }
}

// Keep enough information to retry cleanup if the box is deleted but removing
// its network or port files fails. Never apply a retry to a box that reused a name.
private struct PendingDeletion: Codable {
    let id: String
    let created: String
    let network: String?
    let image: String?
    let imageDigest: String?
}

private func isCopyImage(_ reference: String) -> Bool {
    matches(reference, "^(docker.io/library/)?mybox-copy-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}:snapshot$")
}

extension Backend {
    func deleteBox(_ options: DeleteOptions) throws {
        let id = try containerID(options.name)
        let lock = try networkLock()
        defer { withExtendedLifetime(lock) {} }
        let directory = portDirectory(id)
        let journal = directory.appendingPathComponent("delete.json")
        if exists(portRoot()) { try securePortDirectory(portRoot()) }
        if exists(directory) { try securePortDirectory(directory) }
        let pending = try exists(journal) ? readPortJSON(PendingDeletion.self, journal) : nil
        if let pending {
            guard pending.id == id, !pending.created.isEmpty,
                  pending.network == nil || pending.network == "mybox-net-" + options.name,
                  pending.image == nil || isCopyImage(pending.image!) else {
                throw BoxError("Invalid saved deletion. No box was changed.")
            }
        }

        let allRecords = try records(["list", "--all", "--format", "json"])
        let record = allRecords.first { $0.id == id }
        let deletion: PendingDeletion
        if let record {
            guard record.isManaged else { throw BoxError("\(options.name) is not a box created by MyBox.") }
            try record.checkConfiguration()
            guard ["running", "stopped"].contains(record.state) else {
                throw BoxError("The box is \(record.state). Wait for its current operation to finish.")
            }
            guard let created = record.configuration["creationDate"] as? String, !created.isEmpty,
                  pending == nil || pending?.created == created else {
                throw BoxError("Cannot confirm this box's identity. No box was changed.")
            }
            var network: String?
            if (record.configuration["labels"] as? [String: String])?[networkLabel] != nil {
                let policy = try policy(for: record)
                try checkNetworkMembers(policy, allowed: [id])
                network = policy.network
            }
            let image = record.configuration["image"] as? [String: Any]
            let reference = image?["reference"] as? String
            deletion = PendingDeletion(id: id, created: created, network: network,
                image: reference.flatMap { isCopyImage($0) ? $0 : nil },
                imageDigest: (image?["descriptor"] as? [String: Any])?["digest"] as? String)
        } else if let pending {
            deletion = pending
        } else {
            throw BoxError("No box named \(options.name.debugDescription). Use the full name from mybox list.")
        }

        if !options.confirmed {
            let prompt = record == nil ? "Finish deleting \(options.name.debugDescription)?" :
                "Permanently delete \(options.name.debugDescription) and all its files?"
            FileHandle.standardError.write(Data((prompt + " [y/N] ").utf8))
            let answer = readLine()?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard answer == "y" || answer == "yes" else { print("Deletion canceled."); return }
        }

        if let record {
            try stopManagedBox(record)
            let deadline = Date().addingTimeInterval(5)
            while try forwarderIsRunning(directory) && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            guard try !forwarderIsRunning(directory) else {
                throw BoxError("The port helper has not stopped. The box was not deleted. Retry the command.")
            }
            try securePortDirectory(portRoot())
            try securePortDirectory(directory)
            try writePortJSON(deletion, journal)
            _ = try run(["delete", id], capture: true)
        }

        do {
            if let image = deletion.image {
                let remaining = try self.records(["list", "--all", "--format", "json"])
                let shared = remaining.contains { record in
                    guard let other = record.configuration["image"] as? [String: Any] else { return false }
                    let otherReference = other["reference"] as? String ?? ""
                    let sameReference = otherReference.replacingOccurrences(of: "docker.io/library/", with: "") ==
                        image.replacingOccurrences(of: "docker.io/library/", with: "")
                    let sameDigest = deletion.imageDigest != nil &&
                        (other["descriptor"] as? [String: Any])?["digest"] as? String == deletion.imageDigest
                    return sameReference || sameDigest
                }
                if shared { note("Kept the copy image because another box uses it.") }
                else { _ = try run(["image", "delete", "--force", image], capture: true) }
            }
            if let network = deletion.network {
                let data = try run(["network", "list", "--format", "json"], capture: true)
                guard let networks = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                    throw BoxError("Cannot read saved networks.")
                }
                if networks.contains(where: { $0["id"] as? String == network }) {
                    let info = try networkInfo(network)
                    guard let configuration = info["configuration"] as? [String: Any],
                          (configuration["labels"] as? [String: String])?[managedLabel] == "true" else {
                        throw BoxError("The remaining network is not owned by MyBox.")
                    }
                    let remaining = try self.records(["list", "--all", "--format", "json"])
                    guard !remaining.contains(where: {
                        ($0.configuration["networks"] as? [[String: Any]] ?? []).contains { $0["network"] as? String == network }
                    }) else { throw BoxError("Another container uses the remaining network.") }
                    _ = try run(["network", "delete", network], capture: true)
                }
            }
            try files.removeItem(at: directory)
        } catch {
            throw BoxError("Box \(options.name) was deleted, but cleanup failed: \(error). Run mybox delete \(options.name) --yes to retry cleanup.")
        }
        print("Deleted \(options.name).")
    }
}
