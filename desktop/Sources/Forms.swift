import SwiftUI

struct AppSheetView: View {
    @ObservedObject var model: AppModel
    let sheet: AppSheet
    var body: some View {
        Group {
            switch sheet {
            case .create: CreateBoxView(model: model)
            case .help: CLIHelpView(model: model)
            case .copy(let box): CopyBoxView(model: model, box: box)
            case .resize(let box): ResizeBoxView(model: model, box: box)
            case .ports(let box): PortsView(model: model, box: box)
            case .export(let box): ExportView(model: model, box: box)
            case .command(let box): RunCommandView(model: model, box: box)
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
    @State private var memory = "2"
    @State private var ports: [PortDraft] = []
    private var command: Result<[String], Error> {
        Result { try BoxCommand.create(name: name, cpus: cpus, memory: memory, ports: ports, existing: model.workspaces.map(\.name)) }
    }
    var body: some View {
        SheetFrame(title: "New Box") {
            Form {
                TextField("Name", text: $name, prompt: Text("my-project"))
                TextField("CPU", text: $cpus)
                SizeField(title: "Memory", value: $memory, placeholder: "2")
            }.textFieldStyle(.roundedBorder)
            if !ports.isEmpty {
                ScrollView {
                    VStack(spacing: 10) {
                        ForEach($ports) { $port in
                            HStack(spacing: 8) {
                                TextField("Outside port (Mac)", text: $port.outside).accessibilityLabel("Outside port (Mac)")
                                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                                TextField("Inside port (box)", text: $port.inside).accessibilityLabel("Inside port (box)")
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
                SizeField(title: "Memory", value: $memory, placeholder: SizeInput.gigabytesPlaceholder(box.memory))
                SizeField(title: "Disk", value: $disk, placeholder: SizeInput.gigabytesPlaceholder(box.diskCapacity))
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

struct CLIHelpView: View {
    @ObservedObject var model: AppModel
    @State private var output: String?
    var body: some View {
        SheetFrame(title: "CLI Help", width: 760, dismissal: .close) {
            if let output {
                ScrollView(.vertical) {
                    Text(output.trimmingCharacters(in: .whitespacesAndNewlines))
                        .font(.system(size: 12, design: .monospaced)).lineSpacing(2).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.scrollIndicators(.hidden).frame(height: 440)
            } else { ProgressView().controlSize(.small) }
        } actions: {}
            .task {
                do { output = try await model.read(["help"]) }
                catch { output = error.localizedDescription }
            }
    }
}

struct ValidationMessage: View {
    let text: String
    var body: some View { Text(text).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
}

struct SizeField: View {
    let title: String
    @Binding var value: String
    var placeholder = ""

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 8) {
                TextField(title, text: $value, prompt: Text(placeholder))
                    .labelsHidden().accessibilityLabel("\(title) in gigabytes")
                Text("GB").foregroundStyle(.secondary)
            }
        }
    }
}
