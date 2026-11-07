import SwiftUI

struct BoxesView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 14) {
                Text("Boxes").font(.system(size: 22, weight: .semibold)).tracking(-0.4)
                if model.isDemo { Text("Preview").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).disabled(model.isRefreshing || model.isDemo)
                    .help("Refresh").accessibilityLabel("Refresh boxes").keyboardShortcut("r", modifiers: .command)
                Menu { AppActions(model: model) } label: { Image(systemName: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().help("MyBox").accessibilityLabel("MyBox menu")
                Button { model.sheet = .create } label: { Label("New Box", systemImage: "plus") }
                    .buttonStyle(.borderedProminent).controlSize(.large).disabled(!model.canOpenForms)
                    .keyboardShortcut("n", modifiers: .command)
            }
            if let error = model.connectionError {
                HStack(alignment: .top) {
                    Text(error).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                    Spacer()
                    Button("Settings") { model.sheet = .settings }
                    Button("Retry") { Task { await model.refresh() } }.disabled(model.isRefreshing)
                }.padding(14).background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 8))
            }
            table.overlay {
                if model.workspaces.isEmpty && model.connectionError == nil {
                    if model.hasLoaded { Text("No boxes").font(.callout).foregroundStyle(.secondary) }
                    else { ProgressView().controlSize(.small) }
                }
            }
        }.padding(24).background(AppTheme.background).tint(AppTheme.accent)
            .preferredColorScheme(.dark).frame(minWidth: 960, minHeight: 360)
            .sheet(item: $model.sheet) { AppSheetView(model: model, sheet: $0) }
            .alert("MyBox", isPresented: model.showingActionError) {
                Button("OK") { model.actionError = nil }
            } message: { Text(model.actionError ?? "") }
    }
    private var table: some View {
        Table(model.workspaces, selection: $model.selection) {
            TableColumn("Name") { box in
                HStack(spacing: 10) {
                    Image(systemName: "shippingbox").foregroundStyle(.secondary)
                    Text(box.name).fontWeight(.medium).lineLimit(1).help(box.name)
                }.padding(.vertical, 10)
            }.width(min: 165, ideal: 230)
            TableColumn("State") { BoxState(box: $0) }.width(90)
            TableColumn("CPU", value: \.cores).width(42)
            TableColumn("Memory", value: \.memory).width(74)
            TableColumn("Disk used", value: \.diskUsed).width(78)
            TableColumn("Disk max", value: \.diskCapacity).width(80)
            TableColumn("Ports") { box in
                if box.ports.isEmpty { Text("—").foregroundStyle(.tertiary) }
                else {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(box.ports) { port in
                            HStack(spacing: 6) {
                                LocalhostLink(mapping: port, isEnabled: !model.isDemo)
                                Text("→ \(String(port.inside))")
                                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                            }
                        }
                    }.padding(.vertical, 4)
                }
            }.width(min: 170, ideal: 210)
            TableColumn("") { box in
                HStack(spacing: 8) {
                    BoxTerminalButton(model: model, box: box)
                    Menu { BoxActions(model: model, box: box) } label: {
                        Image(systemName: "ellipsis").frame(width: 22, height: 26).contentShape(Rectangle())
                    }.menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                        .help("Actions for \(box.name)").accessibilityLabel("Actions for \(box.name)")
                }
            }.width(60)
        }.tableStyle(.inset(alternatesRowBackgrounds: false))
            .scrollContentBackground(.hidden)
            .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 10))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(AppTheme.separator))
            .contextMenu(forSelectionType: String.self) { ids in
                if ids.count == 1, let box = model.workspaces.first(where: { ids.contains($0.id) }) {
                    BoxActions(model: model, box: box)
                }
            } primaryAction: { ids in
                if model.canRunCommands, ids.count == 1, let id = ids.first { model.run(["enter", id]) }
            }
    }
}

struct LocalhostLink: View {
    let mapping: PortMapping
    var isEnabled = true
    var body: some View {
        Link(destination: mapping.url) {
            Text("localhost:\(String(mapping.outside))").underline()
                .font(.system(size: 11, design: .monospaced))
        }.foregroundStyle(AppTheme.accent).disabled(!isEnabled)
            .help("Open \(mapping.url.absoluteString)")
            .accessibilityLabel("Open \(mapping.url.absoluteString) in your browser")
    }
}

struct BoxTerminalButton: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var body: some View {
        Button { model.run(["enter", box.name]) } label: {
            Image(systemName: "terminal")
                .frame(width: 24, height: 24).contentShape(Rectangle())
        }.buttonStyle(.borderless).disabled(!model.canRunCommands)
            .help("Open Terminal in \(box.name)")
            .accessibilityLabel("Open Terminal in \(box.name)")
    }
}

struct BoxActions: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var body: some View {
        Button(box.isRunning ? "Stop" : "Start", systemImage: box.isRunning ? "stop" : "play") {
            model.run([box.isRunning ? "stop" : "start", box.name])
        }.disabled(!model.canRunCommands || !["running", "stopped"].contains(box.state))
        Divider()
        Button("Port Mappings…", systemImage: "network") { model.sheet = .ports(box) }.disabled(!model.canOpenForms)
        Button("Import…", systemImage: "square.and.arrow.down") { model.importFiles(to: box) }.disabled(!model.canRunCommands)
        Button("Export…", systemImage: "square.and.arrow.up") { model.sheet = .export(box) }.disabled(!model.canOpenForms)
        Divider()
        Button("Copy…", systemImage: "doc.on.doc") { model.sheet = .copy(box) }.disabled(!model.canOpenForms)
        Button("Resize…", systemImage: "slider.horizontal.3") { model.sheet = .resize(box) }
            .disabled(!model.canOpenForms || box.state != "stopped").help("Stop the box before resizing.")
        Button("Run Command…", systemImage: "chevron.left.forwardslash.chevron.right") { model.sheet = .command(box) }.disabled(!model.canOpenForms)
    }
}

struct AppActions: View {
    @ObservedObject var model: AppModel
    var body: some View {
        Button("Settings…") { model.sheet = .settings }.keyboardShortcut(",", modifiers: .command)
        Divider()
        Button("CLI Help") { model.sheet = .output("CLI Help", ["help"]) }
        Button("CLI Version") { model.sheet = .output("CLI Version", ["--version"]) }
        Button("Copy Zsh Completion") { model.copyCompletion() }
        Divider()
        Button("Quit MyBox") { NSApp.terminate(nil) }.keyboardShortcut("q", modifiers: .command)
    }
}
