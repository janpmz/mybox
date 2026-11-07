import SwiftUI

@main
struct MyBoxDesktopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared
    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            Image(systemName: "shippingbox")
                .accessibilityLabel("MyBox workspaces")
        }.menuBarExtraStyle(.window)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        AppModel.shared.startRefreshing()
        let args = CommandLine.arguments
        if !args.contains("--menu-only") { AppController.shared.showWindow() }
        // An explicit developer option renders actual SwiftUI views for visual
        // QA. It never switches the live app to example data implicitly.
        if let index = args.firstIndex(of: "--screenshot"), args.indices.contains(index + 1) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                let kindIndex = args.firstIndex(of: "--capture-view")
                let kind = kindIndex.flatMap { args.indices.contains($0 + 1) ? args[$0 + 1] : nil } ?? "dashboard"
                AppController.shared.capture(to: args[index + 1], kind: kind)
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        AppController.shared.showWindow(); return true
    }
}

@MainActor
final class AppController: NSObject, NSWindowDelegate {
    static let shared = AppController()
    private var window: NSWindow?
    func showWindow() {
        if window == nil {
            let view = NSHostingView(rootView: BoxesView(model: AppModel.shared))
            let created = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 420),
                                   styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.title = "MyBox"
            created.titlebarAppearsTransparent = true
            created.titleVisibility = .hidden
            created.backgroundColor = NSColor(red: 0.135, green: 0.143, blue: 0.158, alpha: 1)
            created.contentView = view
            created.minSize = NSSize(width: 960, height: 382)
            created.isReleasedWhenClosed = false
            created.delegate = self
            if CommandLine.arguments.contains("--screenshot") {
                created.center()
            } else {
                created.setFrameAutosaveName("MyBoxBoxesWindow")
                if !created.setFrameUsingName("MyBoxBoxesWindow") { created.center() }
            }
            window = created
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
    func capture(to path: String, kind: String) {
        let model = AppModel.shared
        var replacement: AnyView?
        var size = NSSize(width: 1080, height: 420)
        let box = model.workspaces.first ?? Workspace.examples[0]
        switch kind {
        case "menu": replacement = AnyView(MenuBarView(model: model)); size = NSSize(width: MenuBarView.width, height: CGFloat(min(model.workspaces.count, 7)) * 34 + 110)
        case "create": replacement = AnyView(CreateBoxView(model: model)); size = NSSize(width: 420, height: 270)
        case "help": replacement = AnyView(CLIHelpView(model: model)); size = NSSize(width: 760, height: 560)
        case "copy": replacement = AnyView(CopyBoxView(model: model, box: box)); size = NSSize(width: 420, height: 220)
        case "resize": replacement = AnyView(ResizeBoxView(model: model, box: box)); size = NSSize(width: 420, height: 280)
        case "ports": replacement = AnyView(PortsView(model: model, box: box)); size = NSSize(width: 620, height: 300)
        case "export": replacement = AnyView(ExportView(model: model, box: box)); size = NSSize(width: 420, height: 170)
        case "command": replacement = AnyView(RunCommandView(model: model, box: box)); size = NSSize(width: 480, height: 170)
        case "compact": size = NSSize(width: 960, height: 360)
        default: break
        }
        if let replacement { window?.contentView = NSHostingView(rootView: replacement.preferredColorScheme(.dark)) }
        window?.minSize = .zero
        window?.setContentSize(size)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            guard let view = self.window?.contentView,
                  let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { NSApp.terminate(nil); return }
            view.layoutSubtreeIfNeeded()
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try? bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            NSApp.terminate(nil)
        }
    }
}
