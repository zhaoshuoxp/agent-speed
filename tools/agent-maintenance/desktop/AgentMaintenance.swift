import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct Conversation: Identifiable {
    let id: String
    let uuid: String
    let tool: String
    let title: String
    let project: String
    let projectKey: String
    let archived: Bool
    let busy: Bool
    let bytes: Int64
    init(_ value: [String: Any]) {
        uuid = value["id"] as? String ?? ""
        tool = value["tool"] as? String ?? ""
        id = tool + ":" + uuid
        title = value["title"] as? String ?? uuid
        project = value["project"] as? String ?? ""
        projectKey = value["project_id"] as? String ?? value["project_path"] as? String ?? value["cwd"] as? String ?? project
        archived = value["archived"] as? Bool ?? false
        busy = value["busy"] as? Bool ?? false
        bytes = (value["bytes"] as? NSNumber)?.int64Value ?? 0
    }
}

private struct CLIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private enum CLI {
    static func execute(_ arguments: [String]) throws -> [String: Any] {
        let files = FileManager.default
        let candidates = [ProcessInfo.processInfo.environment["AGENT_MAINTENANCE_PYTHON"],
                          "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
                          "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].compactMap { $0 }
        guard let python = candidates.first(where: { files.isExecutableFile(atPath: $0) }),
              let root = Bundle.main.resourceURL?.appendingPathComponent("CLI") else {
            throw CLIError(message: "Python 3 and the bundled maintenance CLI are required.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [root.appendingPathComponent("agent_maintenance.py").path, "--json"] + arguments
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let diagnostics = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CLIError(message: String(data: diagnostics, encoding: .utf8) ?? "The CLI returned no structured result.")
        }
        if process.terminationStatus != 0 {
            let message = object["error"] as? String ?? String(data: data, encoding: .utf8) ?? "Maintenance failed."
            throw CLIError(message: message)
        }
        return object
    }
}

@MainActor
final class MaintenanceModel: ObservableObject {
    @Published var hosts = ["local"]
    @Published var host = "local"
    @Published var conversations: [Conversation] = []
    @Published var selection = Set<String>()
    @Published var project = ""
    @Published var query = ""
    @Published var tool = "all"
    @Published var state = "all"
    @Published var busy = false
    @Published var report = "Select a host and refresh to inspect its conversations."
    @Published var status = "Ready"
    @Published var confirmation = false
    @Published var confirmationText = ""
    @Published var cleanupDays = 30
    private var pendingPlan: URL?
    private var pendingID: String?
    private var pendingUpgrade = false

    var visible: [Conversation] {
        conversations.filter { row in
            (project.isEmpty || row.projectKey == project) && (tool == "all" || row.tool == tool) &&
            (state == "all" || row.archived == (state == "archived")) &&
            (query.isEmpty || (row.title + " " + row.project + " " + row.uuid).localizedCaseInsensitiveContains(query))
        }
    }
    var projects: [(String, String)] {
        var values: [String: String] = [:]
        for row in conversations { values[row.projectKey] = row.project }
        return values.map { ($0.key, $0.value) }.sorted { $0.1.localizedStandardCompare($1.1) == .orderedAscending }
    }
    var selectedRows: [Conversation] { visible.filter { selection.contains($0.id) } }

    func start() {
        run(["hosts"], label: "Loading hosts") { result in
            self.hosts = result["hosts"] as? [String] ?? ["local"]
            self.refresh()
        }
    }
    func refresh() {
        selection = []
        conversations = []
        project = ""
        run(["sessions", "--host", host], label: "Reading " + host) { result in
            let machines = result["hosts"] as? [[String: Any]] ?? []
            self.conversations = (machines.first?["sessions"] as? [[String: Any]] ?? []).map(Conversation.init)
            self.status = "\(self.host): \(self.conversations.count) conversations"
        }
    }
    private func run(_ arguments: [String], label: String, completed: (([String: Any]) -> Void)? = nil) {
        guard !busy else { return }
        busy = true
        status = label
        Task {
            let result = await Task.detached(priority: .utility) { () -> Result<[String: Any], Error> in
                Result { try CLI.execute(arguments) }
            }.value
            busy = false
            switch result {
            case .success(let value):
                let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
                report = data.flatMap { String(data: $0, encoding: .utf8) } ?? "Completed"
                status = "Completed"
                completed?(value)
            case .failure(let error):
                report = error.localizedDescription
                status = "Needs attention"
            }
        }
    }
    private func scope(projectOnly: Bool = false) -> [String]? {
        if projectOnly {
            guard !project.isEmpty else { report = "Choose one project first."; return nil }
            return ["--project", project, "--tool", tool]
        }
        guard !selectedRows.isEmpty else { report = "Select one or more conversations first."; return nil }
        return selectedRows.flatMap { ["--session", $0.uuid] } + ["--tool", tool]
    }
    private func chooseBackup() -> URL? {
        let panel = NSOpenPanel()
        panel.title = "Choose a verified conversation backup"
        panel.allowedContentTypes = [.zip]
        panel.canChooseDirectories = false
        return panel.runModal() == .OK ? panel.url : nil
    }
    func backup(projectOnly: Bool) {
        guard let selection = scope(projectOnly: projectOnly) else { return }
        let panel = NSSavePanel()
        panel.title = "Save and verify conversation backup"
        panel.nameFieldStringValue = host + "-backup-" + String(Int(Date().timeIntervalSince1970)) + ".zip"
        panel.allowedContentTypes = [.zip]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        run(["backup", "--host", host] + selection + ["--output", url.path], label: "Backing up and verifying")
    }
    func plan(_ operation: String) {
        var arguments = ["plan", operation, "--host", host, "--tool", tool]
        if operation == "cleanup" {
            arguments += ["--category", "logs", "--category", "temp", "--older-than", String(cleanupDays)]
        } else {
            guard let selected = scope() else { return }
            arguments += selected
        }
        if operation == "archive" || operation == "delete" {
            guard let backup = chooseBackup() else { return }
            arguments += ["--backup", backup.path]
        }
        let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AgentMaintenance/Plans")
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]) }
        catch { report = error.localizedDescription; return }
        let path = directory.appendingPathComponent(UUID().uuidString + ".json")
        arguments += ["--output", path.path]
        run(arguments, label: "Preparing maintenance preview") { result in
            guard let plan = result["plan"] as? [String: Any], let identifier = plan["plan_id"] as? String else { return }
            self.pendingPlan = path
            self.pendingID = identifier
            self.pendingUpgrade = false
            let sessions = plan["sessions"] as? [[String: Any]] ?? []
            let items = plan["items"] as? [[String: Any]] ?? []
            let bytes = (plan["bytes"] as? NSNumber)?.int64Value ?? 0
            let titles = sessions.prefix(8).compactMap { $0["title"] as? String }.joined(separator: "\n")
            self.confirmationText = "\(operation.capitalized) on \(self.host): \(sessions.count) conversations, \(items.count) files, \(Self.size(bytes)).\n\n\(titles)\n\nPlan: \(path.path)\n\nArchive retains transcript size. Delete removes the originals after rechecking the verified backup."
            self.confirmation = true
        }
    }
    func applyConfirmed() {
        if pendingUpgrade {
            run(["upgrade", "--host", host, "--tool", tool, "--apply"], label: "Updating native CLIs")
        } else if let plan = pendingPlan, let identifier = pendingID {
            run(["apply", plan.path, "--confirm", identifier], label: "Applying reviewed plan") { _ in self.refresh() }
        }
        pendingPlan = nil
        pendingID = nil
        pendingUpgrade = false
    }
    func inspect() { run(["inspect", "--host", host], label: "Measuring storage") }
    func versions() { run(["upgrade", "--host", host, "--tool", tool], label: "Checking CLI versions") }
    func upgrade() {
        pendingUpgrade = true
        confirmationText = "Run the native CLI updater for \(tool) on \(host)? This installs available updates through the official CLI update command."
        confirmation = true
    }
    func verifyOrRestore(restore: Bool) {
        guard let backup = chooseBackup() else { return }
        if !restore { run(["verify", backup.path], label: "Verifying every backup member"); return }
        let panel = NSSavePanel()
        panel.title = "Choose a new directory for restored files"
        panel.nameFieldStringValue = "restored-conversations"
        guard panel.runModal() == .OK, let directory = panel.url else { return }
        run(["restore-files", backup.path, "--output", directory.path], label: "Restoring standalone files")
    }
    static func size(_ bytes: Int64) -> String { ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file) }
}

struct MaintenanceView: View {
    @StateObject private var model = MaintenanceModel()
    var body: some View {
        VStack(spacing: 12) {
            HStack {
                Picker("Host", selection: $model.host) {
                    ForEach(model.hosts, id: \.self) { Text($0).tag($0) }
                }.frame(width: 230)
                Button("Refresh", action: model.refresh)
                Button("Inspect storage", action: model.inspect)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Text(model.status).font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                Picker("Project", selection: $model.project) {
                    Text("All projects").tag("")
                    ForEach(model.projects, id: \.0) { Text($0.1).tag($0.0) }
                }.frame(maxWidth: 330)
                Picker("Tool", selection: $model.tool) {
                    Text("All").tag("all"); Text("Codex").tag("codex"); Text("Claude").tag("claude")
                }.frame(width: 155)
                Picker("State", selection: $model.state) {
                    Text("All").tag("all"); Text("Active").tag("active"); Text("Archived").tag("archived")
                }.frame(width: 175)
                TextField("Search title, project, or UUID", text: $model.query).textFieldStyle(.roundedBorder)
            }
            Table(model.visible, selection: $model.selection) {
                TableColumn("Tool", value: \.tool).width(65)
                TableColumn("Conversation", value: \.title).width(min: 200)
                TableColumn("Project", value: \.project).width(min: 90, ideal: 150)
                TableColumn("State") { row in Text(row.busy ? "Recently active" : row.archived ? "Archived" : "Active") }.width(110)
                TableColumn("Transcript") { row in Text(MaintenanceModel.size(row.bytes)).monospacedDigit() }.width(100)
            }.frame(minHeight: 240)
            HStack {
                Button("Backup selected") { model.backup(projectOnly: false) }
                Button("Backup project") { model.backup(projectOnly: true) }
                Button("Archive") { model.plan("archive") }
                Button("Restore archived") { model.plan("unarchive") }
                Button("Delete") { model.plan("delete") }
                Spacer()
                Menu("Backup tools") {
                    Button("Verify backup") { model.verifyOrRestore(restore: false) }
                    Button("Restore files to a new directory") { model.verifyOrRestore(restore: true) }
                }
            }
            HStack {
                Stepper("Logs and temp older than \(model.cleanupDays) days", value: $model.cleanupDays, in: 1...3650).frame(width: 310)
                Button("Preview cleanup") { model.plan("cleanup") }
                Spacer()
                Button("Check CLI versions", action: model.versions)
                Button("Upgrade CLIs", action: model.upgrade)
            }
            TextEditor(text: .constant(model.report)).font(.system(.caption, design: .monospaced))
                .frame(minHeight: 100, idealHeight: 150, maxHeight: 200).border(Color.secondary.opacity(0.2))
            Text("Backups preserve original files and media. Restore files extracts a standalone copy; native sidebar import is not performed.")
                .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(18).frame(minWidth: 1040, minHeight: 700)
        .disabled(model.busy)
        .onChange(of: model.host) { model.refresh() }
        .alert("Apply this maintenance action?", isPresented: $model.confirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Apply", action: model.applyConfirmed)
        } message: { Text(model.confirmationText) }
        .task { model.start() }
    }
}

@main
struct AgentMaintenanceApp: App {
    var body: some Scene {
        WindowGroup("Agent Maintenance") { MaintenanceView() }
            .defaultSize(width: 1180, height: 780)
    }
}
