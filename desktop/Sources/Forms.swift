import SwiftUI

struct AppSheetView: View {
    @ObservedObject var model: AppModel
    let sheet: AppSheet
    var body: some View {
        Group {
            switch sheet {
            case .create: CreateBoxView(model: model)
            case .createFromFolder: CreateBoxView(model: model, fromFolder: true)
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
    let fromFolder: Bool
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var cpus = "2"
    @State private var memory = "2"
    @State private var ports = PortSelection()
    @State private var folders: [URL]
    @State private var choosingFolders: Bool
    @State private var folderError: String?
    @State private var dropTargeted = false
    @State private var capacity = ResourceCapacity.current()

    init(model: AppModel, fromFolder: Bool = false, previewFolders: [URL] = [], previewSelection: Bool = false) {
        self.model = model
        self.fromFolder = fromFolder
        let preview = model.isDemo ? previewFolders : []
        _folders = State(initialValue: preview)
        _choosingFolders = State(initialValue: fromFolder && (preview.isEmpty || previewSelection))
        if let first = preview.first {
            _name = State(initialValue: FolderInput.suggestedName(for: first, existing: model.workspaces.map(\.name)))
        }
    }

    private var existingNames: [String] { model.workspaces.map(\.name) + Array(model.pendingLifecycle.keys) }
    private var command: Result<[String], Error> {
        Result { try BoxCommand.create(name: name, cpus: cpus, memory: memory, ports: ports.confirmed, existing: existingNames) }
    }
    var body: some View {
        SheetFrame(title: fromFolder ? "New Box from Folder" : "New Box", width: 520) {
            if fromFolder {
                Text(choosingFolders ? "Step 1 of 2 · Choose folders" : "Step 2 of 2 · Configuration")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if choosingFolders {
                folderPicker
            } else {
                Form {
                    TextField("Name", text: $name, prompt: Text("my-project"))
                        .padding(.bottom, 18)
                    ResourceField(title: "CPU", value: $cpus, hint: capacity.cpuHint)
                    ResourceField(title: "Memory [GB]", value: $memory, placeholder: "2",
                                  hint: capacity.memoryHint, trailingHint: capacity.availableMemoryHint)
                }.textFieldStyle(.roundedBorder)
                portEditor
                if !name.isEmpty, case .failure(let error) = command {
                    ValidationMessage(text: error.localizedDescription)
                }
                if model.showsPreviewLabels {
                    Text("Preview mode · Open MyBox normally to create a box.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if !model.isDemo && !model.canRunCommands {
                    ValidationMessage(text: model.connectionError ?? "Waiting for MyBox to connect…")
                }
            }
        } actions: {
            if choosingFolders {
                Button("Continue") {
                    do {
                        folders = try FolderInput.validate(folders)
                        folderError = nil
                        choosingFolders = false
                    } catch { folderError = error.localizedDescription }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(folders.isEmpty)
            } else {
                if fromFolder { Button("Back") { choosingFolders = true } }
                Button("Create box") {
                    if case .success(let arguments) = command,
                       model.run(arguments, importing: folders) { dismiss() }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled((try? command.get()) == nil || !model.canRunCommands || ports.hasDraft || (fromFolder && folders.isEmpty))
            }
        }.task {
            while !Task.isCancelled {
                capacity = ResourceCapacity.current()
                do { try await Task.sleep(for: .seconds(10)) } catch { break }
            }
        }
    }

    private var portEditor: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Port mappings").font(.callout.weight(.medium))
            if !ports.confirmed.isEmpty {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(ports.confirmed) { port in
                            HStack {
                                Text("\(port.outside) → \(port.inside)")
                                    .font(.system(.callout, design: .monospaced))
                                Spacer()
                                Button { ports.remove(port.id) } label: { Image(systemName: "minus.circle") }
                                    .buttonStyle(.borderless).accessibilityLabel("Remove port mapping \(port.outside) to \(port.inside)")
                            }
                        }
                    }.padding(.vertical, 2)
                }.frame(height: min(CGFloat(ports.confirmed.count) * 30, 100))
            }
            HStack(spacing: 8) {
                TextField("Outside port (Mac)", text: $ports.draft.outside).accessibilityLabel("Outside port (Mac)")
                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                TextField("Inside port (box)", text: $ports.draft.inside).accessibilityLabel("Inside port (box)")
                Button("Add") { try? ports.add() }
                    .disabled(ports.validation != nil).accessibilityLabel("Add port mapping")
            }.textFieldStyle(.roundedBorder)
            if !ports.draft.outside.isEmpty && !ports.draft.inside.isEmpty {
                if let error = ports.validation { ValidationMessage(text: error) }
            }
        }
    }

    private var folderPicker: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(spacing: 14) {
                Image(systemName: "folder.badge.plus")
                    .font(.system(size: 32, weight: .light)).foregroundStyle(AppTheme.accent)
                    .accessibilityHidden(true)
                Text(folders.isEmpty ? "Drop folders here" : "Drop more folders here")
                    .font(.headline).multilineTextAlignment(.center)
                Button(folders.isEmpty ? "Choose Folders…" : "Add Folders…") { chooseFolders() }
                    .controlSize(.large)
            }.padding(22).frame(maxWidth: .infinity, minHeight: 170)
                .background(dropTargeted ? AppTheme.accent.opacity(0.12) : AppTheme.surface, in: RoundedRectangle(cornerRadius: 12))
                .overlay {
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(dropTargeted ? AppTheme.accent : AppTheme.secondary.opacity(0.5),
                                      style: StrokeStyle(lineWidth: 1, dash: [6, 4]))
                }
                .dropDestination(for: URL.self) { urls, _ in acceptFolders(urls) } isTargeted: { dropTargeted = $0 }
            if !folders.isEmpty { folderList }
            if let folderError { ValidationMessage(text: folderError) }
            Text(folders.count > 1 ? "Copy the folders into a new box." : "Copy the folder into a new box.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var folderList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(folders, id: \.path) { folder in
                    HStack(spacing: 10) {
                        Image(systemName: "folder").foregroundStyle(AppTheme.accent)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(folder.lastPathComponent).font(.callout.weight(.medium)).lineLimit(1)
                            Text(folder.path)
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                                .help(folder.path)
                        }
                        Spacer(minLength: 0)
                        Button { removeFolder(folder) } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless).foregroundStyle(.secondary)
                            .accessibilityLabel("Remove \(folder.lastPathComponent)")
                    }
                }
            }.padding(.vertical, 2)
        }.frame(height: min(CGFloat(folders.count) * 48, 144))
    }

    private func chooseFolders() {
        let panel = NSOpenPanel()
        panel.title = "Choose Folders for Your New Box"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Add Folders"
        if panel.runModal() == .OK { _ = acceptFolders(panel.urls) }
    }

    private func updateFolders(_ selected: [URL]) {
        if name.isEmpty || folders.first.map({ FolderInput.suggestedName(for: $0, existing: existingNames) == name }) == true {
            name = selected.first.map { FolderInput.suggestedName(for: $0, existing: existingNames) } ?? ""
        }
        folders = selected
        folderError = nil
    }

    private func removeFolder(_ folder: URL) { updateFolders(folders.filter { $0 != folder }) }

    @discardableResult
    private func acceptFolders(_ urls: [URL]) -> Bool {
        do {
            updateFolders(try FolderInput.validate(folders + urls))
            return true
        } catch {
            folderError = error.localizedDescription
            return false
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
    @State private var capacity = ResourceCapacity.current()
    private var current: Workspace { model.workspaces.first { $0.id == box.id } ?? box }
    private var command: Result<[String], Error> {
        Result { try BoxCommand.resize(box: current, cpus: cpus, memory: memory, disk: disk) }
    }
    var body: some View {
        SheetFrame(title: "Resize \(box.name)", width: 520) {
            Form {
                ResourceField(title: "CPU", value: $cpus, placeholder: box.cores, hint: capacity.cpuHint)
                ResourceField(title: "Memory [GB]", value: $memory, placeholder: SizeInput.gigabytesPlaceholder(box.memory),
                              hint: capacity.memoryHint, trailingHint: capacity.availableMemoryHint)
                ResourceField(title: "Max Disk [GB]", value: $disk, placeholder: SizeInput.gigabytesPlaceholder(box.diskCapacity), hint: capacity.diskHint)
            }.textFieldStyle(.roundedBorder)
            if !cpus.isEmpty || !memory.isEmpty || !disk.isEmpty,
               case .failure(let error) = command { ValidationMessage(text: error.localizedDescription) }
        } actions: {
            Button("Resize") {
                if case .success(let args) = command, model.run(args) { dismiss() }
            }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled((try? command.get()) == nil || !model.canRunCommands)
        }.task {
            while !Task.isCancelled {
                capacity = ResourceCapacity.current()
                do { try await Task.sleep(for: .seconds(10)) } catch { break }
            }
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

struct ResourceField: View {
    let title: String
    @Binding var value: String
    var placeholder = ""
    var hint = ""
    var trailingHint = ""

    var body: some View {
        LabeledContent(title) {
            VStack(alignment: .leading, spacing: 5) {
                TextField(title, text: $value, prompt: Text(placeholder))
                    .labelsHidden().accessibilityLabel(title)
                if !hint.isEmpty || !trailingHint.isEmpty {
                    HStack(alignment: .top, spacing: 12) {
                        Text(hint).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        if !trailingHint.isEmpty {
                            Text(trailingHint).multilineTextAlignment(.trailing)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }.font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
}
