import SwiftUI

/// The headings and rows use the same widths; only the name column expands.
private enum BoxColumns {
    static let spacing: CGFloat = 24
    static let compute: CGFloat = 130
    static let disk: CGFloat = 130
    static let ports: CGFloat = 240
    static let actions: CGFloat = 168
    // Reserve room even when macOS uses a persistent (non-overlay) scrollbar.
    static let scrollGutter: CGFloat = 16
    static func nameWidth(in width: CGFloat) -> CGFloat {
        width - 2 * AppTheme.contentInset - 4 * spacing - compute - disk - ports - actions - scrollGutter
    }
}

struct BoxesView: View {
    @ObservedObject var model: AppModel
    @State private var filter = ""
    @FocusState private var filterFocused: Bool
    @FocusState private var focusedBox: String?

    private var visibleBoxes: [Workspace] {
        model.workspaces.filter { filter.isEmpty || $0.name.localizedStandardContains(filter) }
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            if let error = model.connectionError {
                HStack(alignment: .top, spacing: 16) {
                    Text(error).font(.callout).foregroundStyle(AppTheme.secondary).textSelection(.enabled)
                    Spacer()
                    Button("Retry") { Task { await model.refresh() } }.disabled(model.isRefreshing)
                }.padding(.horizontal, AppTheme.contentInset).padding(.vertical, 12)
            }
            GeometryReader { geometry in
                let nameWidth = BoxColumns.nameWidth(in: geometry.size.width)
                VStack(spacing: 0) {
                    headings(nameWidth: nameWidth)
                    Hairline().padding(.leading, AppTheme.contentInset)
                        .padding(.trailing, AppTheme.contentInset + BoxColumns.scrollGutter)
                    list(nameWidth: nameWidth)
                }
            }
            footer
        }
        .background(AppTheme.background).foregroundStyle(AppTheme.primary).tint(AppTheme.accent)
        .preferredColorScheme(.dark).frame(minWidth: 1040, minHeight: 330)
        .sheet(item: $model.sheet) { AppSheetView(model: model, sheet: $0) }
        .alert("MyBox", isPresented: model.showingActionError) {
            Button("OK") { model.actionError = nil }
        } message: { Text(model.actionError ?? "") }
        .alert("Delete “\(model.boxToDelete?.name ?? "")”?", isPresented: model.showingDeleteConfirmation,
               presenting: model.boxToDelete) { box in
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) { model.run(["delete", box.name, "--yes"]) }
                .disabled(!model.canRunCommands)
        } message: { _ in
            Text("This permanently deletes the box and all its files. A running box will be stopped.")
        }
    }

    private var toolbar: some View {
        HStack(spacing: 16) {
            Image(systemName: "cube").font(.system(size: 21, weight: .light)).accessibilityHidden(true)
            Text("Boxes").font(.system(size: 16, weight: .semibold))
            Text("\(model.workspaces.filter(\.isRunning).count) running")
                .font(.system(size: 13)).foregroundStyle(AppTheme.secondary)
            if model.showsPreviewLabels { Text("Preview").font(.caption).foregroundStyle(AppTheme.secondary) }
            Spacer(minLength: 24)
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").font(.system(size: 15)).accessibilityHidden(true)
                TextField("Filter boxes…", text: $filter)
                    .textFieldStyle(.plain).font(.system(size: 13)).focused($filterFocused)
                    .accessibilityLabel("Filter boxes")
                    .onExitCommand { filter = ""; filterFocused = false }
            }.foregroundStyle(AppTheme.secondary).padding(6).frame(width: 210)
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(filterFocused ? AppTheme.accent : .clear, lineWidth: 1))
            Button { Task { await model.refresh() } } label: { ActionIcon(symbol: "arrow.clockwise") }
                .buttonStyle(FlatActionStyle()).disabled(model.isRefreshing || model.isDemo)
                .help("Refresh boxes").accessibilityLabel("Refresh boxes").keyboardShortcut("r", modifiers: .command)
            Button { model.sheet = .create } label: {
                HStack(spacing: 7) {
                    Image(systemName: "plus")
                        .font(.system(size: 12, weight: .semibold))
                        .frame(width: 14, height: 14).accessibilityHidden(true)
                    Text("New Box")
                }.fixedSize()
            }.buttonStyle(PrimaryActionStyle())
                .disabled(!model.canOpenForms).keyboardShortcut("n", modifiers: .command)
        }.padding(.horizontal, AppTheme.contentInset).frame(height: 52).background(AppTheme.header)
    }

    private func headings(nameWidth: CGFloat) -> some View {
        HStack(spacing: BoxColumns.spacing) {
            Text("Name").frame(width: nameWidth, alignment: .leading)
            Text("Compute").frame(width: BoxColumns.compute, alignment: .leading)
            Text("Disk used").frame(width: BoxColumns.disk, alignment: .leading)
            Text("Port forwarding").frame(width: BoxColumns.ports, alignment: .leading)
            Color.clear.frame(width: BoxColumns.actions)
        }.font(.system(size: 12)).foregroundStyle(AppTheme.secondary)
            .frame(height: 40).padding(.horizontal, AppTheme.contentInset)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func list(nameWidth: CGFloat) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(visibleBoxes.enumerated()), id: \.element.id) { index, box in
                        if index > 0 {
                            Hairline().frame(width: nameWidth + 4 * BoxColumns.spacing + BoxColumns.compute + BoxColumns.disk + BoxColumns.ports + BoxColumns.actions)
                        }
                        row(box, nameWidth: nameWidth).id(box.id)
                    }
                    Color.clear.frame(height: 20).accessibilityHidden(true)
                }.padding(.horizontal, AppTheme.contentInset).frame(maxWidth: .infinity, alignment: .leading)
            }.overlay {
                if visibleBoxes.isEmpty && model.connectionError == nil {
                    if !model.hasLoaded { ProgressView().controlSize(.small) }
                    else {
                        Text(filter.isEmpty ? "No boxes" : "No boxes match “\(filter)”")
                            .font(.callout).foregroundStyle(AppTheme.secondary)
                    }
                }
            }
            .onChange(of: model.selection) { _, id in
                if let id { proxy.scrollTo(id) }
            }
            .onChange(of: focusedBox) { _, id in
                if let id { model.selection = id; proxy.scrollTo(id) }
            }
        }
    }

    private func row(_ box: Workspace, nameWidth: CGFloat) -> some View {
        HStack(spacing: BoxColumns.spacing) {
            HStack(spacing: 12) {
                StateDot(box: box)
                VStack(alignment: .leading, spacing: 4) {
                    Text(box.name).font(.system(size: 14, weight: .semibold)).lineLimit(1).help(box.name)
                    BoxStatusLabel(model: model, box: box)
                }
            }.frame(width: nameWidth, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(box.cores == "-" ? "—" : "\(box.cores) \(box.cores == "1" ? "core" : "cores")")
                Text(box.memory == "-" ? "—" : "\(box.memory) RAM")
                    .font(AppTheme.secondaryFont).foregroundStyle(AppTheme.secondary)
            }.frame(width: BoxColumns.compute, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(box.diskUsed)
                Text(box.diskCapacity == "-" ? "Capacity unavailable" : "of \(box.diskCapacity)")
                    .font(AppTheme.secondaryFont).foregroundStyle(AppTheme.secondary)
            }.frame(width: BoxColumns.disk, alignment: .leading)
            VStack(alignment: .leading, spacing: 6) {
                if box.ports.isEmpty { Text("—").foregroundStyle(AppTheme.secondary) }
                ForEach(box.ports) { port in
                    PortTag(mapping: port, isRunning: box.isRunning, isEnabled: !model.isDemo)
                }
            }.frame(width: BoxColumns.ports, alignment: .leading)
            HStack(spacing: 10) {
                BoxStartStopButton(model: model, box: box, showsTitle: true)
                BoxTerminalButton(model: model, box: box)
                BoxOverflowMenu(model: model, box: box)
            }.frame(width: BoxColumns.actions, alignment: .trailing)
        }.font(AppTheme.rowFont).monospacedDigit()
            .padding(.vertical, 12).frame(minHeight: AppTheme.rowHeight)
            .contentShape(Rectangle())
            // Keep keyboard navigation without giving clicked rows a selected appearance.
            .focusable().focusEffectDisabled().focused($focusedBox, equals: box.id)
            .onTapGesture(count: 2) { if model.canRunCommands { model.run(["enter", box.name]) } }
            .onTapGesture { model.selection = box.id; focusedBox = box.id }
            .onKeyPress(.return) {
                guard focusedBox == box.id, model.canRunCommands else { return .ignored }
                model.run(["enter", box.name]); return .handled
            }
            .onMoveCommand { direction in
                guard let index = visibleBoxes.firstIndex(where: { $0.id == box.id }) else { return }
                let next = direction == .down ? index + 1 : direction == .up ? index - 1 : index
                if visibleBoxes.indices.contains(next) { focusedBox = visibleBoxes[next].id }
            }
            .contextMenu { BoxActions(model: model, box: box) }
            .accessibilityElement(children: .contain).accessibilityLabel(box.name)
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Hairline()
            HStack(spacing: 12) {
                Circle().fill(model.connectionError != nil || !model.hasLoaded || model.showsPreviewLabels ? AppTheme.stopped : AppTheme.running)
                    .frame(width: 6, height: 6).accessibilityHidden(true)
                Text(model.showsPreviewLabels ? "Preview data" : model.connectionError != nil ? "Connection unavailable" : model.hasLoaded ? "Connected to MyBox" : "Connecting…")
                Spacer()
                Text("Running boxes: \(model.hasLoaded ? WorkspaceTotals(boxes: model.workspaces).runningMemory : "—") RAM")
                    .help("Configured memory assigned to running boxes, not measured memory use.")
                Text("Disk used: \(model.hasLoaded ? WorkspaceTotals(boxes: model.workspaces).diskUsed : "—")")
                    .padding(.leading, 20).help("Actual disk usage across all boxes, including stopped boxes.")
                Menu { AppActions(model: model) } label: { ActionIcon(symbol: "gearshape") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .padding(.leading, 8).help("MyBox options").accessibilityLabel("MyBox options")
            }.font(AppTheme.footerFont).foregroundStyle(AppTheme.secondary).monospacedDigit()
                .padding(.horizontal, AppTheme.contentInset).frame(height: 36)
        }.background(AppTheme.footer)
    }
}

struct BoxStatusLabel: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var body: some View {
        Text(model.pendingLifecycle[box.id].map { $0 == "start" ? "Start requested…" : "Stop requested…" } ?? box.stateLabel)
            .font(AppTheme.secondaryFont)
            .foregroundStyle(box.isRunning ? AppTheme.running : AppTheme.secondary)
            .lineLimit(1)
    }
}

struct PortTag: View {
    let mapping: PortMapping
    let isRunning: Bool
    var isEnabled: Bool
    var body: some View {
        Group {
            if isEnabled {
                Link(destination: mapping.url) { tag }
                    .buttonStyle(FlatActionStyle())
                    .help("Open \(mapping.url.absoluteString)\(isRunning ? "" : " (box is stopped)")")
                    .accessibilityLabel("Open localhost:\(mapping.outside), forwarded to port \(mapping.inside)")
            } else {
                // Preview mappings remain legible without enabling browser actions.
                tag.accessibilityElement(children: .ignore)
                    .accessibilityLabel("localhost:\(mapping.outside), forwarded to port \(mapping.inside)")
            }
        }
    }

    private var tag: some View {
        HStack(spacing: 5) {
            Text("localhost:\(String(mapping.outside))").foregroundStyle(isRunning ? AppTheme.accent : AppTheme.secondary)
            Text("→ \(String(mapping.inside))").foregroundStyle(AppTheme.secondary)
            if isRunning { Image(systemName: "arrow.up.right.square").foregroundStyle(AppTheme.accent) }
        }.font(.system(size: 12, design: .monospaced)).fixedSize()
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(isRunning ? AppTheme.activePort : AppTheme.inactivePort, in: RoundedRectangle(cornerRadius: 5))
            .contentShape(RoundedRectangle(cornerRadius: 5))
    }
}

struct BoxOverflowMenu: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var body: some View {
        Menu { BoxActions(model: model, box: box) } label: { ActionIcon(symbol: "ellipsis") }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("Actions for \(box.name)").accessibilityLabel("Actions for \(box.name)")
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

struct BoxStartStopButton: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var showsTitle = false
    var body: some View {
        Button { model.run([box.isRunning ? "stop" : "start", box.name]) } label: {
            HStack(spacing: 8) {
                if model.pendingLifecycle[box.id] != nil {
                    ProgressView().controlSize(.mini).frame(width: 14, height: 14)
                } else {
                    Image(systemName: box.isRunning ? "stop" : "play").font(.system(size: AppTheme.iconSize))
                }
                if showsTitle { Text(box.isRunning ? "Stop" : "Start").font(.system(size: 13)) }
            }.frame(width: showsTitle ? 84 : 28, height: 28).contentShape(Rectangle())
        }.buttonStyle(FlatActionStyle())
            .disabled(!model.canRunCommands || model.pendingLifecycle[box.id] != nil || !["running", "stopped"].contains(box.state))
            .help("\(box.isRunning ? "Stop" : "Start") \(box.name)")
            .accessibilityLabel("\(box.isRunning ? "Stop" : "Start") \(box.name)")
    }
}

struct BoxTerminalButton: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    var body: some View {
        Button { model.run(["enter", box.name]) } label: {
            ActionIcon(symbol: "terminal")
        }.buttonStyle(FlatActionStyle()).disabled(!model.canRunCommands || model.pendingLifecycle[box.id] != nil)
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
        }.disabled(!model.canRunCommands || model.pendingLifecycle[box.id] != nil || !["running", "stopped"].contains(box.state))
        Divider()
        Button("Port Mappings", systemImage: "network") { model.sheet = .ports(box) }.disabled(!model.canOpenForms)
        Button("Import", systemImage: "square.and.arrow.down") { model.importFiles(to: box) }.disabled(!model.canRunCommands)
        Button("Export", systemImage: "square.and.arrow.up") { model.sheet = .export(box) }.disabled(!model.canOpenForms)
        Divider()
        Button("Copy", systemImage: "doc.on.doc") { model.sheet = .copy(box) }.disabled(!model.canOpenForms)
        Button("Resize", systemImage: "slider.horizontal.3") { model.sheet = .resize(box) }
            .disabled(!model.canOpenForms || box.state != "stopped").help("Stop the box before resizing.")
        Button("Run Command", systemImage: "chevron.left.forwardslash.chevron.right") { model.sheet = .command(box) }.disabled(!model.canOpenForms)
        Divider()
        Button("Delete", systemImage: "trash", role: .destructive) { model.boxToDelete = box }
            .disabled(!model.canRunCommands || !["running", "stopped"].contains(box.state))
    }
}

struct AppActions: View {
    @ObservedObject var model: AppModel
    var body: some View {
        Button("CLI Help") { model.sheet = .help }
        Divider()
        Button("Quit MyBox") { NSApp.terminate(nil) }.keyboardShortcut("q", modifiers: .command)
    }
}
