import SwiftUI

struct PortsView: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    @State private var ports: [PortStatus] = []
    @State private var draft = PortDraft()
    @State private var error: String?
    @State private var warning: String?
    @State private var isLoading = false
    @State private var hasLoaded = false

    private var mapping: PortMapping? { try? draft.mapping() }
    private var current: Workspace { model.workspaces.first { $0.id == box.id } ?? box }
    private var duplicate: Bool { mapping.map { port in ports.contains { $0.id == port.outside } } ?? false }
    var body: some View {
        SheetFrame(title: "Port Mappings · \(box.name)", width: 620, dismissal: .close) {
            Table(ports) {
                TableColumn("Outside port (Mac)") { port in
                    LocalhostLink(mapping: port.mapping, isEnabled: !model.isDemo)
                }.width(min: 150, ideal: 170)
                TableColumn("Inside port (box)") { Text(String($0.mapping.inside)).monospacedDigit() }.width(135)
                TableColumn("State") { Text($0.state.capitalized).foregroundStyle(.secondary) }.width(65)
                TableColumn("Owner", value: \.owner).width(55)
                TableColumn("") { port in
                    if port.fixed {
                        Image(systemName: "lock").foregroundStyle(.tertiary).help("This original mapping cannot be removed.")
                    } else {
                        Button { model.run(["ports", "remove", box.name, "--outside-port", String(port.mapping.outside)]) } label: {
                            Image(systemName: "minus.circle")
                        }.buttonStyle(.borderless).disabled(!model.canRunCommands)
                            .help("Remove outside port \(port.mapping.outside)").accessibilityLabel("Remove outside port \(port.mapping.outside)")
                    }
                }.width(25)
            }.tableStyle(.inset(alternatesRowBackgrounds: false)).scrollContentBackground(.hidden)
                .frame(height: max(110, min(240, CGFloat(ports.count) * 36 + 40)))
                .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 8))
                .overlay {
                    if ports.isEmpty {
                        if isLoading { ProgressView().controlSize(.small) }
                        else if hasLoaded { Text("No mappings").font(.callout).foregroundStyle(.secondary) }
                    }
                }
            if let error {
                HStack {
                    ValidationMessage(text: error)
                    Spacer()
                    Button("Retry") { Task { await refresh() } }.disabled(isLoading)
                }
            }
            if current.isRunning, let warning { ValidationMessage(text: warning) }
            HStack(spacing: 10) {
                TextField("Outside port (Mac)", text: $draft.outside).accessibilityLabel("Outside port (Mac)")
                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                TextField("Inside port (box)", text: $draft.inside).accessibilityLabel("Inside port (box)")
                Button("Add") {
                    if let mapping, model.run(["ports", "add", box.name, "--inside-port", String(mapping.inside), "--outside-port", String(mapping.outside)]) {
                        draft = PortDraft()
                    }
                }.disabled(mapping == nil || duplicate || !model.canRunCommands || !hasLoaded || error != nil)
            }.textFieldStyle(.roundedBorder)
            if duplicate { ValidationMessage(text: "That outside port is already mapped.") }
            else if !draft.outside.isEmpty && !draft.inside.isEmpty && mapping == nil {
                ValidationMessage(text: "Ports must be between 1 and 65535.")
            }
        } actions: {}.task {
            while !Task.isCancelled {
                await refresh()
                do { try await Task.sleep(for: .seconds(8)) } catch { break }
            }
        }
    }

    private func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        if model.isDemo {
            ports = box.ports.map { PortStatus(mapping: $0, owner: "MyBox", state: box.isRunning ? "active" : "saved", fixed: false) }
            hasLoaded = true
            return
        }
        do {
            let list = try PortList.parse(await model.read(["ports", "list", box.name]))
            ports = list.ports; warning = list.forwardingWarning(isRunning: current.isRunning); error = nil; hasLoaded = true
        } catch { self.error = error.localizedDescription }
    }
}
