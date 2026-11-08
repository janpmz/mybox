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
private final class BoxesWindow: NSWindow {
    override var backingScaleFactor: CGFloat {
        // Keep demo captures at Retina resolution even on a 1× display.
        CommandLine.arguments.contains("--screenshot") ? 2 : super.backingScaleFactor
    }

    override func resignKey() {
        super.resignKey()
        // The custom filter outline should also disappear when another window is clicked.
        if let editor = firstResponder as? NSTextView, editor.isFieldEditor {
            makeFirstResponder(nil)
        }
    }

    override func sendEvent(_ event: NSEvent) {
        // Blank areas and buttons don't necessarily take keyboard focus on macOS.
        // End filter editing before forwarding an outside click to its normal target.
        if [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(event.type),
           let editor = firstResponder as? NSTextView, editor.isFieldEditor,
           let field = editor.delegate as? NSTextField, field.window === self,
           !field.bounds.contains(field.convert(event.locationInWindow, from: nil)) {
            makeFirstResponder(nil)
        }
        super.sendEvent(event)
    }
}

@MainActor
final class AppController: NSObject, NSWindowDelegate {
    static let shared = AppController()
    private var window: NSWindow?
    func showWindow() {
        if window == nil {
            let view = NSHostingView(rootView: BoxesView(model: AppModel.shared))
            let created = BoxesWindow(contentRect: NSRect(x: 0, y: 0, width: 1160, height: 370),
                                   styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.title = "MyBox"
            created.titlebarAppearsTransparent = true
            created.titleVisibility = .visible
            created.backgroundColor = AppTheme.headerColor
            created.contentView = view
            centerTitle(in: created)
            created.contentMinSize = NSSize(width: 1040, height: 330)
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
        // Opening the list shouldn't automatically start editing the filter.
        window?.makeFirstResponder(nil)
    }

    private func centerTitle(in window: NSWindow) {
        // Recent macOS versions left-align the standard title. Place a native
        // label in the existing title bar, retaining its traffic lights and drag area.
        guard let close = window.standardWindowButton(.closeButton), let titlebar = close.superview else { return }
        let title = NSTextField(labelWithString: window.title)
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.textColor = .secondaryLabelColor
        title.alignment = .center
        title.translatesAutoresizingMaskIntoConstraints = false
        window.titleVisibility = .hidden
        titlebar.addSubview(title)
        NSLayoutConstraint.activate([
            title.centerXAnchor.constraint(equalTo: titlebar.centerXAnchor),
            title.centerYAnchor.constraint(equalTo: close.centerYAnchor)
        ])
    }

    func capture(to path: String, kind: String) {
        let model = AppModel.shared
        var replacement: AnyView?
        var size = NSSize(width: 1160, height: 370)
        let box = model.workspaces.first ?? Workspace.examples[0]
        switch kind {
        case "menu": replacement = AnyView(MenuBarView(model: model)); size = NSSize(width: MenuBarView.width, height: MenuBarView.height(boxCount: model.workspaces.count))
        case "create": replacement = AnyView(CreateBoxView(model: model)); size = NSSize(width: 420, height: 270)
        case "help": replacement = AnyView(CLIHelpView(model: model)); size = NSSize(width: 760, height: 560)
        case "copy": replacement = AnyView(CopyBoxView(model: model, box: box)); size = NSSize(width: 420, height: 220)
        case "resize": replacement = AnyView(ResizeBoxView(model: model, box: box)); size = NSSize(width: 420, height: 280)
        case "ports": replacement = AnyView(PortsView(model: model, box: box)); size = NSSize(width: 620, height: 300)
        case "export": replacement = AnyView(ExportView(model: model, box: box)); size = NSSize(width: 420, height: 170)
        case "command": replacement = AnyView(RunCommandView(model: model, box: box)); size = NSSize(width: 480, height: 170)
        case "compact": size = NSSize(width: 1040, height: 330)
        default: break
        }
        if let replacement { window?.contentView = NSHostingView(rootView: replacement.preferredColorScheme(.dark)) }
        window?.minSize = .zero
        window?.setContentSize(size)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            let captureView = kind == "window" ? self.window?.contentView?.superview : self.window?.contentView
            guard let view = captureView else { NSApp.terminate(nil); return }
            view.layoutSubtreeIfNeeded()
            guard let bitmap = self.screenshotBitmap(of: view) else { NSApp.terminate(nil); return }
            let output = kind == "window" ? self.windowScreenshot(bitmap, size: view.bounds.size) : bitmap
            try? output?.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            NSApp.terminate(nil)
        }
    }

    private func screenshotBitmap(of view: NSView) -> NSBitmapImageRep? {
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil,
                                           pixelsWide: Int((view.bounds.width * 2).rounded()),
                                           pixelsHigh: Int((view.bounds.height * 2).rounded()),
                                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                           isPlanar: false, colorSpaceName: .deviceRGB,
                                           bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        bitmap.size = view.bounds.size
        view.cacheDisplay(in: view.bounds, to: bitmap)
        return bitmap
    }

    private func windowScreenshot(_ bitmap: NSBitmapImageRep, size: NSSize) -> NSBitmapImageRep? {
        // View caching includes the rectangular backing behind the native window corners.
        // Clip the export on a transparent canvas so it fits both light and dark READMEs.
        let margin: CGFloat = 12
        let scale = CGFloat(bitmap.pixelsWide) / size.width
        let canvas = CGSize(width: size.width + 2 * margin, height: size.height + 2 * margin)
        guard let source = bitmap.cgImage,
              let context = CGContext(data: nil, width: Int((canvas.width * scale).rounded()),
                                      height: Int((canvas.height * scale).rounded()),
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: source.colorSpace ?? CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        context.scaleBy(x: scale, y: scale)
        let frame = CGRect(origin: CGPoint(x: margin, y: margin), size: size)
        context.addPath(CGPath(roundedRect: frame, cornerWidth: 12, cornerHeight: 12, transform: nil))
        context.clip()
        context.draw(source, in: frame)
        guard let image = context.makeImage() else { return nil }
        let output = NSBitmapImageRep(cgImage: image)
        output.size = canvas
        return output
    }
}
