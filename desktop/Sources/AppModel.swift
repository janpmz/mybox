import AppKit
import SwiftUI

enum AppSheet: Identifiable {
    case create, settings
    case copy(Workspace), resize(Workspace), ports(Workspace), export(Workspace), command(Workspace)
    case output(String, [String])
    var id: String {
        switch self {
        case .create: return "create"
        case .settings: return "settings"
        case .copy(let box): return "copy-" + box.id
        case .resize(let box): return "resize-" + box.id
        case .ports(let box): return "ports-" + box.id
        case .export(let box): return "export-" + box.id
        case .command(let box): return "command-" + box.id
        case .output(let title, _): return title
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
    let isDemo: Bool
    private var refreshTask: Task<Void, Never>?

    init(demo: Bool = CommandLine.arguments.contains("--demo")) {
        isDemo = demo
        if demo { workspaces = Workspace.examples; hasLoaded = true }
    }
    var canRunCommands: Bool {
        !isDemo && connectionError == nil && hasLoaded && MyBoxClient.resolveExecutable() != nil
    }
    var canOpenForms: Bool { isDemo || canRunCommands }
    var showingActionError: Binding<Bool> {
        Binding(get: { self.actionError != nil }, set: { if !$0 { self.actionError = nil } })
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
            connectionError = "Choose the MyBox CLI in Settings."
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
    func run(_ arguments: [String]) -> Bool {
        guard !isDemo else { return false }
        guard let executable = MyBoxClient.resolveExecutable() else {
            actionError = "Choose the MyBox CLI in Settings."
            return false
        }
        do {
            try MyBoxClient.openTerminal(executable: executable, arguments: arguments)
            return true
        } catch { actionError = error.localizedDescription; return false }
    }
    func read(_ arguments: [String]) async throws -> String {
        guard let executable = MyBoxClient.resolveExecutable() else {
            throw DesktopError("Choose the MyBox CLI in Settings.")
        }
        return try await Task.detached(priority: .utility) {
            try MyBoxClient.capture(executable: executable, arguments: arguments)
        }.value
    }
    func copyCompletion() {
        Task {
            do {
                let script = try await read(["completion", "zsh"])
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(script, forType: .string)
            } catch { actionError = error.localizedDescription }
        }
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
