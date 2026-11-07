import SwiftUI

struct MenuBarView: View {
    static let width: CGFloat = 324
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("MyBox").font(.system(size: 14, weight: .semibold))
                if model.isDemo { Text("Preview").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                Button { AppController.shared.showWindow(); model.sheet = .create } label: { Image(systemName: "plus") }
                    .buttonStyle(.borderless).disabled(!model.canOpenForms).help("New box").accessibilityLabel("New box")
            }
            if model.connectionError != nil {
                Button("Connection unavailable") { AppController.shared.showWindow() }.buttonStyle(.plain).foregroundStyle(.secondary)
            } else if model.workspaces.isEmpty {
                Text(model.hasLoaded ? "No boxes" : "Loading…").font(.callout).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(spacing: 2) {
                        ForEach(model.workspaces) { box in
                            HStack(spacing: 10) {
                                Circle().fill(box.isRunning ? AppTheme.running : Color.secondary).frame(width: 5, height: 5)
                                Button {
                                    model.selection = box.id
                                    AppController.shared.showWindow()
                                } label: { Text(box.name).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()) }
                                    .buttonStyle(.plain).lineLimit(1).help("\(box.name) · \(box.stateLabel)")
                                BoxBrowserButton(box: box, isEnabled: !model.isDemo)
                                BoxTerminalButton(model: model, box: box)
                                Menu { BoxActions(model: model, box: box) } label: { Image(systemName: "ellipsis") }
                                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().accessibilityLabel("Actions for \(box.name)")
                            }.font(.system(size: 12)).padding(.vertical, 4)
                        }
                    }
                }.frame(height: CGFloat(min(model.workspaces.count, 7)) * 34)
            }
            Divider()
            HStack {
                Button { AppController.shared.showWindow() } label: {
                    Label("Open MyBox", systemImage: "macwindow")
                }.buttonStyle(.borderless).help("Open the full MyBox window")
                Spacer()
                Menu { AppActions(model: model) } label: { Image(systemName: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().accessibilityLabel("MyBox menu")
            }.font(.system(size: 12))
        }.padding(16).frame(width: Self.width).background(AppTheme.background).preferredColorScheme(.dark).tint(AppTheme.accent)
            .onChange(of: model.sheet?.id) { _, id in
                if id != nil { AppController.shared.showWindow() }
            }
            .onChange(of: model.actionError) { _, error in
                if error != nil { AppController.shared.showWindow() }
            }
    }
}

struct BoxBrowserButton: View {
    let box: Workspace
    var isEnabled = true
    var body: some View {
        Group {
            if box.ports.count == 1, let port = box.ports.first {
                Link(destination: port.url) { icon }
                    .buttonStyle(.borderless).help("Open \(port.url.absoluteString)")
                    .accessibilityLabel("Open \(box.name) in your browser at localhost:\(port.outside)")
            } else if !box.ports.isEmpty {
                Menu {
                    ForEach(box.ports) { port in
                        Link("localhost:\(String(port.outside))", destination: port.url)
                    }
                } label: { icon }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .help("Open a website in \(box.name)")
                    .accessibilityLabel("Choose a localhost address for \(box.name)")
            } else {
                Color.clear.frame(width: 24, height: 24).accessibilityHidden(true)
            }
        }.disabled(!isEnabled)
    }
    private var icon: some View {
        Image(systemName: "safari").font(.system(size: 14))
            .frame(width: 24, height: 24).contentShape(Rectangle())
    }
}
