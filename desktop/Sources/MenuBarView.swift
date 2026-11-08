import SwiftUI

struct MenuBarView: View {
    static let width: CGFloat = 400
    static let rowHeight: CGFloat = 56
    private static let headerAndFooterHeight: CGFloat = 80
    private static var availableListHeight: CGFloat {
        let screen = NSScreen.screens.first { $0.frame.contains(NSEvent.mouseLocation) } ?? NSScreen.main
        // Leave space for the popover shell and keep the footer on screen.
        let available = (screen?.visibleFrame.height ?? 600) - headerAndFooterHeight - 32
        return max(rowHeight, floor(available / rowHeight) * rowHeight)
    }
    static func height(boxCount: Int) -> CGFloat {
        min(CGFloat(max(1, boxCount)) * rowHeight, availableListHeight) + headerAndFooterHeight
    }
    @ObservedObject var model: AppModel
    @State private var maximumListHeight = MenuBarView.availableListHeight

    var body: some View {
        VStack(spacing: 0) {
            header
            if model.connectionError != nil {
                Button("Connection unavailable") { AppController.shared.showWindow() }
                    .buttonStyle(FlatActionStyle()).foregroundStyle(AppTheme.secondary)
                    .frame(maxWidth: .infinity).frame(height: Self.rowHeight)
            } else if model.workspaces.isEmpty {
                Text(model.hasLoaded ? "No boxes" : "Loading…")
                    .font(.callout).foregroundStyle(AppTheme.secondary)
                    .frame(maxWidth: .infinity).frame(height: Self.rowHeight)
            } else {
                if CGFloat(model.workspaces.count) * Self.rowHeight <= maximumListHeight {
                    rows
                } else {
                    ScrollView(.vertical) { rows }
                        .scrollIndicators(.automatic)
                        .frame(height: maximumListHeight)
                }
            }
            footer
        }.frame(width: Self.width).background(AppTheme.background).foregroundStyle(AppTheme.primary)
            .preferredColorScheme(.dark).tint(AppTheme.accent)
            .onAppear { maximumListHeight = Self.availableListHeight }
            .onChange(of: model.sheet?.id) { _, id in
                if id != nil { AppController.shared.showWindow() }
            }
            .onChange(of: model.actionError) { _, error in
                if error != nil { AppController.shared.showWindow() }
            }
            .onChange(of: model.boxToDelete?.id) { _, id in
                if id != nil { AppController.shared.showWindow() }
            }
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(Array(model.workspaces.enumerated()), id: \.element.id) { index, box in
                row(box).overlay(alignment: .top) {
                    if index > 0 { Hairline() }
                }
            }
        }.padding(.horizontal, 16)
    }

    private var header: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text("MyBox").font(.system(size: 14, weight: .semibold))
                if model.isDemo { Text("Preview").font(.caption).foregroundStyle(AppTheme.secondary) }
                Spacer()
                Button { AppController.shared.showWindow(); model.sheet = .create } label: { ActionIcon(symbol: "plus") }
                    .buttonStyle(FlatActionStyle()).foregroundStyle(AppTheme.accent)
                    .disabled(!model.canOpenForms).help("New box").accessibilityLabel("New box")
            }.padding(.horizontal, 16).frame(height: 38)
            Hairline()
        }.background(AppTheme.header)
    }

    private func row(_ box: Workspace) -> some View {
        HStack(spacing: 8) {
            StateDot(box: box)
            BoxStartStopButton(model: model, box: box).foregroundStyle(AppTheme.accent)
            Button {
                model.selection = box.id
                AppController.shared.showWindow()
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text(box.name).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                    BoxStatusLabel(model: model, box: box)
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).help("\(box.name) · \(box.stateLabel)")
                .accessibilityLabel("Open \(box.name), \(box.stateLabel), in MyBox")
            HStack(spacing: 4) {
                BoxBrowserButton(box: box, isEnabled: !model.isDemo).foregroundStyle(AppTheme.accent)
                BoxTerminalButton(model: model, box: box)
                BoxOverflowMenu(model: model, box: box).foregroundStyle(AppTheme.accent)
            }
        }.frame(height: Self.rowHeight)
            .contextMenu { BoxActions(model: model, box: box) }
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Hairline()
            HStack {
                Button { AppController.shared.showWindow() } label: {
                    Label("Open MyBox", systemImage: "macwindow").padding(.vertical, 6)
                }.buttonStyle(FlatActionStyle()).help("Open the full MyBox window")
                Spacer()
                Menu { AppActions(model: model) } label: { ActionIcon(symbol: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .help("MyBox menu").accessibilityLabel("MyBox menu")
            }.font(AppTheme.footerFont).foregroundStyle(AppTheme.accent)
                .padding(.horizontal, 16).frame(height: 40)
        }.background(AppTheme.footer)
    }
}

struct BoxBrowserButton: View {
    let box: Workspace
    var isEnabled = true
    var body: some View {
        Group {
            if box.ports.count == 1, let port = box.ports.first {
                Link(destination: port.url) { icon }
                    .buttonStyle(FlatActionStyle()).help("Open \(port.url.absoluteString)")
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
                icon.foregroundStyle(AppTheme.secondary).opacity(0.4)
                    .help("No port mappings for \(box.name)")
                    .accessibilityLabel("No browser address for \(box.name)")
            }
        }.disabled(!isEnabled)
    }
    private var icon: some View { ActionIcon(symbol: "arrow.up.right.square") }
}
