import SwiftUI

struct AppSheetView: View {
    @ObservedObject var model: AppModel
    let sheet: AppSheet
    var body: some View {
        Group {
            switch sheet {
            case .create: CreateBoxView(model: model)
            case .settings: SettingsView(model: model)
            case .copy(let box): CopyBoxView(model: model, box: box)
            case .resize(let box): ResizeBoxView(model: model, box: box)
            case .ports(let box): PortsView(model: model, box: box)
            case .export(let box): ExportView(model: model, box: box)
            case .command(let box): RunCommandView(model: model, box: box)
            case .output(let title, let args): CLIOutputView(model: model, title: title, arguments: args)
            }
        }.preferredColorScheme(.dark)
            .alert("MyBox", isPresented: model.showingActionError) {
                Button("OK") { model.actionError = nil }
            } message: { Text(model.actionError ?? "") }
    }
}

struct CreateBoxView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var cpus = "2"
    @State private var memory = "2g"
    @State private var ports: [PortDraft] = []
    private var command: Result<[String], Error> {
        Result { try BoxCommand.create(name: name, cpus: cpus, memory: memory, ports: ports, existing: model.workspaces.map(\.name)) }
    }
    var body: some View {
        SheetFrame(title: "New Box") {
            Form {
                TextField("Name", text: $name, prompt: Text("my-project"))
                TextField("CPU", text: $cpus)
                TextField("Memory", text: $memory, prompt: Text("2g"))
            }.textFieldStyle(.roundedBorder)
            if !ports.isEmpty {
                ScrollView {
                    VStack(spacing: 10) {
                        ForEach($ports) { $port in
                            HStack(spacing: 8) {
                                TextField("Mac port", text: $port.outside).accessibilityLabel("Mac port")
                                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                                TextField("Box port", text: $port.inside).accessibilityLabel("Box port")
                                Button { ports.removeAll { $0.id == port.id } } label: { Image(systemName: "minus.circle") }
                                    .buttonStyle(.borderless).accessibilityLabel("Remove port mapping")
                            }
                        }
                    }.padding(.vertical, 2)
                }.frame(height: min(CGFloat(ports.count) * 36, 150))
            }
            Button("Add port mapping", systemImage: "plus") { ports.append(PortDraft()) }
                .buttonStyle(.borderless).disabled(ports.count >= 64)
            if !name.isEmpty, case .failure(let error) = command {
                ValidationMessage(text: error.localizedDescription)
            }
        } actions: {
            Button("Create") {
                if case .success(let arguments) = command, model.run(arguments) { dismiss() }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled((try? command.get()) == nil || !model.canRunCommands)
        }
    }
}

struct CopyBoxView: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    private var command: Result<[String], Error> {
        Result { try BoxCommand.copy(source: box.name, name: name, existing: model.workspaces.map(\.name)) }
    }
    var body: some View {
        SheetFrame(title: "Copy \(box.name)") {
            TextField("New name", text: $name, prompt: Text("\(box.name)-copy")).textFieldStyle(.roundedBorder)
            Text("Includes saved credentials. Port mappings are not copied.").font(.caption).foregroundStyle(.secondary)
            if !name.isEmpty, case .failure(let error) = command { ValidationMessage(text: error.localizedDescription) }
        } actions: {
            Button("Copy") {
                if case .success(let args) = command, model.run(args) { dismiss() }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled((try? command.get()) == nil || !model.canRunCommands)
        }
    }
}

struct ResizeBoxView: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    @Environment(\.dismiss) private var dismiss
    @State private var cpus = ""
    @State private var memory = ""
    @State private var disk = ""
    private var current: Workspace { model.workspaces.first { $0.id == box.id } ?? box }
    private var command: Result<[String], Error> {
        Result { try BoxCommand.resize(box: current, cpus: cpus, memory: memory, disk: disk) }
    }
    var body: some View {
        SheetFrame(title: "Resize \(box.name)") {
            Form {
                TextField("CPU", text: $cpus, prompt: Text(box.cores))
                TextField("Memory (GB)", text: $memory, prompt: Text(box.memory))
                TextField("Disk (GB)", text: $disk, prompt: Text(box.diskCapacity))
            }.textFieldStyle(.roundedBorder)
            Text("Blank fields stay unchanged.").font(.caption).foregroundStyle(.secondary)
            if !cpus.isEmpty || !memory.isEmpty || !disk.isEmpty,
               case .failure(let error) = command { ValidationMessage(text: error.localizedDescription) }
        } actions: {
            Button("Resize") {
                if case .success(let args) = command, model.run(args) { dismiss() }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled((try? command.get()) == nil || !model.canRunCommands)
        }
    }
}

struct ExportView: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    @Environment(\.dismiss) private var dismiss
    @State private var path = ""
    var body: some View {
        SheetFrame(title: "Export from \(box.name)") {
            TextField("Path in /workspace", text: $path, prompt: Text("/workspace/my-project"))
                .textFieldStyle(.roundedBorder).accessibilityLabel("Path in workspace")
            if !path.isEmpty && !BoxCommand.validExportPath(path) {
                ValidationMessage(text: "Choose a path inside /workspace.")
            }
        } actions: {
            Button("Choose destination…") {
                dismiss()
                DispatchQueue.main.async { model.exportFiles(from: box, path: path) }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(!BoxCommand.validExportPath(path) || !model.canRunCommands)
        }
    }
}

struct RunCommandView: View {
    @ObservedObject var model: AppModel
    let box: Workspace
    @Environment(\.dismiss) private var dismiss
    @State private var command = ""
    var body: some View {
        SheetFrame(title: "Run in \(box.name)", width: 480) {
            TextField("Shell command", text: $command, prompt: Text("npm run dev"))
                .textFieldStyle(.roundedBorder).font(.system(.body, design: .monospaced)).accessibilityLabel("Shell command")
        } actions: {
            Button("Run in Terminal") {
                if let args = BoxCommand.command(box: box.name, text: command), model.run(args) { dismiss() }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(BoxCommand.command(box: box.name, text: command) == nil || !model.canRunCommands)
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @AppStorage("cliPath") private var cliPath = ""
    @State private var error: String?
    var body: some View {
        SheetFrame(title: "Settings", width: 460, dismissal: .close) {
            VStack(alignment: .leading, spacing: 12) {
                Text("MyBox CLI").fontWeight(.medium)
                Text(MyBoxClient.resolveExecutable() ?? "Not found").font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Choose…") {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = false
                        panel.allowsMultipleSelection = false
                        if panel.runModal() == .OK, let url = panel.url {
                            if FileManager.default.isExecutableFile(atPath: url.path) {
                                cliPath = url.path; error = nil; Task { await model.refresh() }
                            } else { error = "Choose an executable file." }
                        }
                    }
                    Button("Use default") { cliPath = ""; Task { await model.refresh() } }
                }
                if let error { ValidationMessage(text: error) }
            }
        } actions: {}
    }
}

struct CLIOutputView: View {
    @ObservedObject var model: AppModel
    let title: String
    let arguments: [String]
    @State private var output: String?
    var body: some View {
        SheetFrame(title: title, width: title == "CLI Help" ? 760 : 420, dismissal: .close) {
            if let output {
                ScrollView([.horizontal, .vertical]) {
                    Text(output.trimmingCharacters(in: .whitespacesAndNewlines))
                        .font(.system(size: 12, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: title == "CLI Help" ? 440 : 70)
            } else { ProgressView().controlSize(.small) }
        } actions: {}
            .task {
                do { output = try await model.read(arguments) }
                catch { output = error.localizedDescription }
            }
    }
}

struct ValidationMessage: View {
    let text: String
    var body: some View { Text(text).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
}
