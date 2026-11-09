import AppKit
import SwiftUI

enum AppSheet: Identifiable {
    case create, createFromFolder, help
    case copy(Workspace), resize(Workspace), ports(Workspace), export(Workspace), command(Workspace)
    var id: String {
        switch self {
        case .create: return "create"
        case .createFromFolder: return "create-from-folder"
        case .help: return "help"
        case .copy(let box): return "copy-" + box.id
        case .resize(let box): return "resize-" + box.id
        case .ports(let box): return "ports-" + box.id
        case .export(let box): return "export-" + box.id
        case .command(let box): return "command-" + box.id
        }
    }
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()
    @Published var workspaces: [Workspace] = []
    @Published var selection: String?
    @Published var isRefreshing = false
    @Published var connectionError: String?
    @Published var actionError: String?
    @Published var hasLoaded = false
    @Published var sheet: AppSheet?
    @Published var boxToDelete: Workspace?
    @Published private(set) var pendingLifecycle: [String: String] = [:]
    let isDemo: Bool
    var showsPreviewLabels: Bool { isDemo && !CommandLine.arguments.contains("--screenshot") }
    private var refreshTask: Task<Void, Never>?

    init(demo: Bool = CommandLine.arguments.contains("--demo")) {
        isDemo = demo
        if demo {
            workspaces = Workspace.examples
            hasLoaded = true
            if let index = CommandLine.arguments.firstIndex(of: "--demo-scenario"), CommandLine.arguments.indices.contains(index + 1) {
                configurePreview(CommandLine.arguments[index + 1])
            }
        }
    }

    private func configurePreview(_ scenario: String) {
        switch scenario {
        case "empty": workspaces = []
        case "stopped":
            workspaces = workspaces.map {
                Workspace(name: $0.name, state: "stopped", cores: $0.cores, memory: $0.memory,
                          diskUsed: $0.diskUsed, diskCapacity: $0.diskCapacity, ports: $0.ports,
                          memoryBytes: $0.memoryBytes, diskUsedBytes: $0.diskUsedBytes)
            }
        case "many":
            workspaces = (0..<18).map { index in
                Workspace(name: index == 0 ? "a-project-with-an-unusually-long-descriptive-name" : "project-\(index)",
                          state: index.isMultiple(of: 2) ? "running" : "stopped", cores: "6", memory: "8 GB",
                          diskUsed: "2.3 GB", diskCapacity: "550.8 GB",
                          ports: index == 0 ? (0..<4).map { PortMapping(inside: 8080 + $0, outside: 10000 + $0) } : [],
                          memoryBytes: 8 * bytesPerMemoryGB, diskUsedBytes: 2_300_000_000)
            }
        case "unknown":
            workspaces = [Workspace(name: "resources-unavailable", state: "starting", cores: "-", memory: "-",
                                    diskUsed: "-", diskCapacity: "-", ports: [])]
        case "error": connectionError = "The MyBox service could not be reached. Check the service, then try again."
        default: break
        }
    }
    var canRunCommands: Bool {
        !isDemo && connectionError == nil && hasLoaded && MyBoxClient.resolveExecutable() != nil
    }
    var canOpenForms: Bool { isDemo || canRunCommands }
    var showingActionError: Binding<Bool> {
        Binding(get: { self.actionError != nil }, set: { if !$0 { self.actionError = nil } })
    }
    var showingDeleteConfirmation: Binding<Bool> {
        Binding(get: { self.boxToDelete != nil }, set: { if !$0 { self.boxToDelete = nil } })
    }
    func startRefreshing() {
        guard refreshTask == nil, !isDemo else { return }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(8))
            }
        }
    }
    func refresh() async {
        guard !isDemo, !isRefreshing else { return }
        guard let executable = MyBoxClient.resolveExecutable() else {
            connectionError = "The MyBox CLI was not found. Run make build in the MyBox project folder."
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            workspaces = try await MyBoxClient.list(executable: executable)
            if !workspaces.contains(where: { $0.id == selection }) { selection = nil }
            hasLoaded = true
            connectionError = nil
        } catch { connectionError = error.localizedDescription }
    }
    @discardableResult
    func run(_ arguments: [String], importing folders: [URL] = []) -> Bool {
        guard !isDemo else { return false }
        // All entry points share this guard, including row menus and double-click.
        if arguments.count > 1, pendingLifecycle[arguments[1]] != nil { return false }
        guard let executable = MyBoxClient.resolveExecutable() else {
            actionError = "The MyBox CLI was not found. Run make build in the MyBox project folder."
            return false
        }
        do {
            if !folders.isEmpty {
                guard arguments.count >= 2, arguments[0] == "create" else {
                    throw DesktopError("Choose a new box before copying folders.")
                }
            }
            var selectedFolders: [URL] = []
            if !folders.isEmpty {
                selectedFolders = try FolderInput.validate(folders)
            }
            let lifecycle = arguments.count == 2 && ["start", "stop"].contains(arguments[0])
            let completion = try MyBoxClient.openTerminal(executable: executable, arguments: arguments,
                                                         observeCompletion: lifecycle || !selectedFolders.isEmpty,
                                                         importing: selectedFolders)
            if let completion {
                let name = arguments[1], action = arguments[0]
                pendingLifecycle[name] = action
                Task { [weak self] in
                    while !Task.isCancelled {
                        if let status = try? String(contentsOf: completion, encoding: .utf8), let code = Int(status.trimmingCharacters(in: .whitespacesAndNewlines)) {
                            try? FileManager.default.removeItem(at: completion.deletingLastPathComponent())
                            await self?.refresh()
                            self?.pendingLifecycle[name] = nil
                            if code != 0 {
                                self?.actionError = !folders.isEmpty
                                    ? "Could not finish setting up \(name). See Terminal for the failed step and instructions to retry (exit \(code))."
                                    : "Could not \(action) \(name). See Terminal for the command's error (exit \(code))."
                            }
                            break
                        }
                        do { try await Task.sleep(for: .milliseconds(500)) } catch { break }
                    }
                }
            }
            return true
        } catch { actionError = error.localizedDescription; return false }
    }
    func read(_ arguments: [String]) async throws -> String {
        guard let executable = MyBoxClient.resolveExecutable() else {
            throw DesktopError("The MyBox CLI was not found. Run make build in the MyBox project folder.")
        }
        return try await Task.detached(priority: .utility) {
            try MyBoxClient.capture(executable: executable, arguments: arguments)
        }.value
    }
    func importFiles(to box: Workspace) {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.title = "Import to \(box.name)"
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Import"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        run(["import", box.name, url.path])
    }
    func exportFiles(from box: Workspace, path: String) {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSSavePanel()
        panel.title = "Export from \(box.name)"
        panel.message = "Choose a new destination."
        panel.nameFieldStringValue = (path as NSString).lastPathComponent
        panel.canCreateDirectories = true
        panel.prompt = "Export"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard !FileManager.default.fileExists(atPath: url.path) else {
            actionError = "The destination already exists. Choose a new name."
            return
        }
        run(["export", box.name, path, url.path])
    }
}
