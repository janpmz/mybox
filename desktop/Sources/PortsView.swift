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
    private var duplicate: Bool { mapping.map { port in ports.contains { $0.id == port.outside } } ?? false }
    var body: some View {
        SheetFrame(title: "Ports · \(box.name)", width: 540, dismissal: .close) {
            Table(ports) {
                TableColumn("Mac") { port in
                    LocalhostLink(mapping: port.mapping, isEnabled: !model.isDemo)
                }.width(min: 140, ideal: 150)
                TableColumn("Box") { Text(String($0.mapping.inside)).monospacedDigit() }.width(55)
                TableColumn("State") { Text($0.state.capitalized).foregroundStyle(.secondary) }.width(65)
                TableColumn("Owner", value: \.owner).width(55)
                TableColumn("") { port in
                    if port.fixed {
                        Image(systemName: "lock").foregroundStyle(.tertiary).help("This original mapping cannot be removed.")
                    } else {
                        Button { model.run(["ports", "remove", box.name, "--outside-port", String(port.mapping.outside)]) } label: {
                            Image(systemName: "minus.circle")
                        }.buttonStyle(.borderless).disabled(!model.canRunCommands)
                            .help("Remove port \(port.mapping.outside)").accessibilityLabel("Remove port \(port.mapping.outside)")
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
            if let warning { ValidationMessage(text: warning) }
            HStack(spacing: 10) {
                TextField("Mac port", text: $draft.outside).accessibilityLabel("Mac port")
                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                TextField("Box port", text: $draft.inside).accessibilityLabel("Box port")
                Button("Add") {
                    if let mapping, model.run(["ports", "add", box.name, "--inside-port", String(mapping.inside), "--outside-port", String(mapping.outside)]) {
                        draft = PortDraft()
                    }
                }.disabled(mapping == nil || duplicate || !model.canRunCommands || !hasLoaded || error != nil)
            }.textFieldStyle(.roundedBorder)
            if duplicate { ValidationMessage(text: "That Mac port is already mapped.") }
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
            ports = list.ports; warning = list.warning; error = nil; hasLoaded = true
        } catch { self.error = error.localizedDescription }
    }
}
