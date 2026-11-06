import AppKit
import Darwin
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
    let isSubagent: Bool
    let bytes: Int64
    init(_ value: [String: Any]) {
        uuid = value["id"] as? String ?? ""
        tool = value["tool"] as? String ?? ""
        id = tool + ":" + uuid
        title = value["title"] as? String ?? uuid
        project = value["project"] as? String ?? ""
        projectKey = value["project_path"] as? String ?? value["cwd"] as? String ?? value["project_id"] as? String ?? project
        archived = value["archived"] as? Bool ?? false
        busy = value["busy"] as? Bool ?? false
        isSubagent = value["is_subagent"] as? Bool ?? false
        bytes = (value["bytes"] as? NSNumber)?.int64Value ?? 0
    }
}

private struct CLIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct CLIUpgradeRequest {
    let hosts: [String]
    let tool: String
    func arguments(apply: Bool) throws -> [String] {
        guard !hosts.isEmpty else { throw CLIError(message: "Select at least one CLI target first.") }
        return ["upgrade", "--tool", tool] + hosts.flatMap { ["--host", $0] } + (apply ? ["--apply"] : [])
    }
}

struct CLIVersionRow: Identifiable {
    let host: String
    let tool: String
    let installed: String
    let latest: String
    let state: String
    let detail: String
    var id: String { host + ":" + tool }
}

private enum CLI {
    static func execute(_ arguments: [String], allowPartialResults: Bool = false) throws -> [String: Any] {
        guard let root = Bundle.main.resourceURL?.appendingPathComponent("CLI") else {
            throw CLIError(message: "The bundled maintenance CLI is missing.")
        }
        let finder = Process()
        finder.executableURL = URL(fileURLWithPath: "/bin/bash")
        finder.arguments = [root.appendingPathComponent("find-python.sh").path]
        let found = Pipe()
        let finderErrors = Pipe()
        finder.standardOutput = found
        finder.standardError = finderErrors
        try finder.run()
        let path = found.fileHandleForReading.readDataToEndOfFile()
        let problem = finderErrors.fileHandleForReading.readDataToEndOfFile()
        finder.waitUntilExit()
        guard finder.terminationStatus == 0, let value = String(data: path, encoding: .utf8),
              !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw CLIError(message: String(data: problem, encoding: .utf8) ?? "Python 3.9+ is required.")
        }
        let python = value.trimmingCharacters(in: .whitespacesAndNewlines)
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
            if allowPartialResults, object["hosts"] != nil { return object }
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
    @Published var includeSubagents = false
    @Published var showSources = false
    @Published var autoHosts = true
    @Published var hostDraft = ""
    @Published var codexDraft = ""
    @Published var claudeDraft = ""
    @Published var discoveredHosts: [String] = []
    @Published var sourceSummary = "Loading data sources"
    @Published var sourceDetail = ""
    @Published var scanNotice = ""
    @Published var executionHost = "This computer"
    @Published var showCLIs = false
    @Published var upgradeServers = Set<String>()
    @Published var cliScope = "servers"
    @Published var cliTool = "all"
    @Published var cliRows: [CLIVersionRow] = []
    @Published var cliSummary = "Choose targets, then check installed versions."
    @Published var cliConfirmation = false
    @Published var cliConfirmationText = ""
    private var confirmedUpgrade: CLIUpgradeRequest?
    private var pendingPlan: URL?
    private var pendingID: String?
    private var pendingOperation = ""

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
        run(["config"], label: "Finding SSH hosts and local sources") { result in
            self.readConfiguration(result)
            self.refresh()
        }
    }
    func refresh() {
        guard !busy else { return }
        selection = []
        conversations = []
        project = ""
        var arguments = ["sessions", "--host", host]
        if includeSubagents { arguments += ["--include-subagents"] }
        run(arguments, label: "Reading " + host) { result in
            let machines = result["hosts"] as? [[String: Any]] ?? []
            self.conversations = (machines.first?["sessions"] as? [[String: Any]] ?? []).map(Conversation.init)
            if let diagnostics = machines.first?["diagnostics"] as? [String: Any] {
                self.readDiagnostics(diagnostics)
            }
            self.status = "\(self.host): \(self.conversations.count) conversations"
        }
    }
    private func readConfiguration(_ value: [String: Any]) {
        hosts = value["hosts"] as? [String] ?? ["local"]
        if !hosts.contains(host) { host = "local" }
        discoveredHosts = value["discovered_ssh_hosts"] as? [String] ?? []
        upgradeServers = Set(value["upgrade_hosts"] as? [String] ?? hosts.filter { $0 != "local" })
        autoHosts = !(value["explicit_hosts"] as? Bool ?? false)
        hostDraft = hosts.filter { $0 != "local" }.joined(separator: "\n")
        let roots = value["configured_data_roots"] as? [String: String] ?? [:]
        codexDraft = roots["codex"] ?? ""
        claudeDraft = roots["claude"] ?? ""
        if host == "local", let diagnostics = value["local_diagnostics"] as? [String: Any] { readDiagnostics(diagnostics) }
    }
    private func readDiagnostics(_ value: [String: Any]) {
        let codex = value["codex"] as? [String: Any] ?? [:]
        let claude = value["claude"] as? [String: Any] ?? [:]
        let codexCount = codex["total_records"] as? Int ?? 0
        let claudeCount = claude["total_records"] as? Int ?? 0
        let recordHost = value["hostname"] as? String ?? host
        if host == "local" { executionHost = recordHost }
        sourceSummary = "Codex: \(codexCount) records  ·  Claude: \(claudeCount) records"
        sourceDetail = "App computer: \(executionHost)  ·  Record host: \(recordHost)\nCodex: \(codex["root"] as? String ?? "Unknown")\nClaude: \(claude["root"] as? String ?? "Unknown")"
        let errors = value["errors"] as? [[String: Any]] ?? []
        if !errors.isEmpty {
            scanNotice = errors.compactMap { $0["error"] as? String }.joined(separator: "; ")
        } else if codexCount == 0 {
            scanNotice = "No Codex records found at \(codex["root"] as? String ?? "the selected directory"). Check Sources & SSH."
        } else if (codex["subagent_records"] as? Int ?? 0) == codexCount && codexCount > 0 {
            scanNotice = "This host contains Codex subagent records only. Orphan records are listed; Include subagents shows every record."
        } else if (codex["visible_records"] as? Int ?? 0) == 0 && !includeSubagents {
            scanNotice = "Codex records are present. Enable Include subagents to show records hidden by the conversation filter."
        } else {
            scanNotice = ""
        }
    }
    func openSources() {
        run(["config"], label: "Reading source settings") { result in
            self.readConfiguration(result)
            self.showSources = true
        }
    }
    func saveSources() {
        var arguments = ["config", "--codex-home", codexDraft, "--claude-home", claudeDraft]
        arguments += autoHosts ? ["--auto-hosts"] : ["--hosts", hostDraft]
        arguments += ["--upgrade-hosts", upgradeServers.sorted().joined(separator: ",")]
        run(arguments, label: "Saving source settings") { result in
            self.readConfiguration(result)
            self.showSources = false
            self.refresh()
        }
    }
    func chooseDirectory(codex: Bool) {
        let panel = NSOpenPanel()
        panel.title = codex ? "Choose the Codex data home" : "Choose the Claude data home"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.showsHiddenFiles = true
        if panel.runModal() == .OK, let path = panel.url?.path {
            if codex { codexDraft = path } else { claudeDraft = path }
        }
    }
    private func run(_ arguments: [String], label: String, allowPartialResults: Bool = false,
                     completed: (([String: Any]) -> Void)? = nil) {
        guard !busy else { return }
        busy = true
        status = label
        Task {
            let result = await Task.detached(priority: .utility) { () -> Result<[String: Any], Error> in
                Result { try CLI.execute(arguments, allowPartialResults: allowPartialResults) }
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
        if operation == "archive" {
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
            self.pendingOperation = operation
            let sessions = plan["sessions"] as? [[String: Any]] ?? []
            let items = plan["items"] as? [[String: Any]] ?? []
            let bytes = (plan["bytes"] as? NSNumber)?.int64Value ?? 0
            let titles = sessions.prefix(8).compactMap { $0["title"] as? String }.joined(separator: "\n")
            let note = operation == "delete"
                ? "Delete permanently removes these conversations and their descendants. Manage any backups separately; no backup is checked by this action."
                : "Archive retains transcript size."
            self.confirmationText = "\(operation.capitalized) on \(self.host): \(sessions.count) records including descendants, \(items.count) files, \(Self.size(bytes)).\n\n\(titles)\n\n\(note)"
            self.confirmation = true
        }
    }
    func applyConfirmed() {
        if let plan = pendingPlan, let identifier = pendingID {
            run(["apply", plan.path, "--confirm", identifier], label: "Applying reviewed plan") { _ in self.refresh() }
        }
        pendingPlan = nil
        pendingID = nil
        pendingOperation = ""
    }
    var confirmationTitle: String { pendingOperation == "delete" ? "Delete selected conversations?" : "Apply maintenance action?" }
    var confirmationAction: String { pendingOperation == "delete" ? "Delete" : "Apply" }
    func inspect() { run(["inspect", "--host", host], label: "Measuring storage") }
    var cliTargets: [String] {
        switch cliScope {
        case "computer": return ["local"]
        case "conversation": return [host]
        default: return hosts.filter { $0 != "local" && upgradeServers.contains($0) }
        }
    }
    func openCLIs() {
        showCLIs = true
        versions()
    }
    func versions() {
        executeCLIRequest(CLIUpgradeRequest(hosts: cliTargets, tool: cliTool), apply: false)
    }
    private func executeCLIRequest(_ request: CLIUpgradeRequest, apply: Bool) {
        do {
            let arguments = try request.arguments(apply: apply)
            run(arguments, label: apply ? "Updating target CLIs" : "Checking target CLI versions", allowPartialResults: true) { value in
                self.cliRows = []
                for machine in value["hosts"] as? [[String: Any]] ?? [] {
                    let name = machine["host"] as? String ?? "Unknown"
                    for entry in machine["tools"] as? [[String: Any]] ?? [] {
                        let raw = entry["state"] as? String ?? "Unknown"
                        let state = raw == "installed" ? ((entry["update_available"] as? Bool ?? false) ? "Update available" : "Up to date") : raw
                        self.cliRows.append(CLIVersionRow(host: name, tool: entry["tool"] as? String ?? "",
                            installed: entry["installed"] as? String ?? "—", latest: entry["latest"] as? String ?? "—",
                            state: state, detail: entry["error"] as? String ?? entry["note"] as? String ?? entry["path"] as? String ?? ""))
                    }
                }
                let failures = value["failures"] as? [[String: Any]] ?? []
                for failure in failures where failure["tool"] == nil {
                    self.cliRows.append(CLIVersionRow(host: failure["host"] as? String ?? "Unknown", tool: request.tool,
                        installed: "—", latest: "—", state: "Connection error", detail: failure["error"] as? String ?? ""))
                }
                self.cliSummary = "\(request.hosts.count) targets checked; \(failures.count) errors. " + (apply ? "Native update results are shown below." : "No upgrade was performed.")
            }
        } catch {
            cliSummary = error.localizedDescription
        }
    }
    func upgrade() {
        let request = CLIUpgradeRequest(hosts: cliTargets, tool: cliTool)
        guard !request.hosts.isEmpty else { cliSummary = "Select at least one CLI target first."; return }
        confirmedUpgrade = request
        cliConfirmationText = "Update \(cliTool) using each target's native CLI updater?\n\nTargets: \(request.hosts.joined(separator: ", "))\n\nVersions at or above the release channel are skipped. No CLI is installed on targets where it is missing."
        cliConfirmation = true
    }
    func applyCLIUpgrade() {
        guard let request = confirmedUpgrade else { return }
        executeCLIRequest(request, apply: true)
        confirmedUpgrade = nil
    }
    func saveUpgradeTargets() {
        run(["config", "--upgrade-hosts", upgradeServers.sorted().joined(separator: ",")], label: "Saving CLI targets") { value in
            self.readConfiguration(value)
            self.cliSummary = "CLI target selection saved."
        }
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
                Button("Sources & SSH", action: model.openSources)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Text(model.status).font(.callout).foregroundStyle(.secondary)
            }
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(model.sourceSummary).font(.callout.weight(.medium))
                    Text(model.sourceDetail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    if !model.scanNotice.isEmpty {
                        Text(model.scanNotice).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    }
                }
                Spacer()
                Toggle("Include subagents", isOn: $model.includeSubagents).toggleStyle(.checkbox)
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
                TableColumn("Kind") { row in Text(row.isSubagent ? "Subagent" : "Conversation") }.width(95)
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
                Text("CLI targets: \(model.upgradeServers.count) SSH servers").font(.caption).foregroundStyle(.secondary)
                Button("Remote CLI maintenance", action: model.openCLIs)
            }
            TextEditor(text: .constant(model.report)).font(.system(.caption, design: .monospaced))
                .frame(minHeight: 100, idealHeight: 150, maxHeight: 200).border(Color.secondary.opacity(0.2))
            Text("Delete uses your manual backup policy and does not request a backup file. Restore files extracts a standalone copy.")
                .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(18).frame(minWidth: 1040, minHeight: 700)
        .disabled(model.busy)
        .onChange(of: model.host) { model.refresh() }
        .onChange(of: model.includeSubagents) { model.refresh() }
        .sheet(isPresented: $model.showSources) { SourceSettingsView(model: model) }
        .sheet(isPresented: $model.showCLIs) { CLIMaintenanceView(model: model) }
        .alert(model.confirmationTitle, isPresented: $model.confirmation) {
            Button("Cancel", role: .cancel) {}
            if model.confirmationAction == "Delete" {
                Button("Delete", role: .destructive, action: model.applyConfirmed)
            } else {
                Button("Apply", action: model.applyConfirmed)
            }
        } message: { Text(model.confirmationText) }
        .task { model.start() }
    }
}

private struct CLIMaintenanceView: View {
    @ObservedObject var model: MaintenanceModel
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Remote CLI maintenance").font(.title2.weight(.semibold))
                Spacer()
                Text("App computer: " + model.executionHost).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Picker("Targets", selection: $model.cliScope) {
                    Text("Selected SSH servers").tag("servers")
                    Text("Conversation host: " + model.host).tag("conversation")
                    Text("This computer").tag("computer")
                }.frame(width: 360)
                Picker("CLIs", selection: $model.cliTool) {
                    Text("Codex + Claude").tag("all")
                    Text("Codex").tag("codex")
                    Text("Claude").tag("claude")
                }.frame(width: 240)
            }
            if model.cliScope == "servers" {
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], alignment: .leading) {
                    ForEach(model.hosts.filter { $0 != "local" }, id: \.self) { name in
                        Toggle(name, isOn: Binding(get: { model.upgradeServers.contains(name) }, set: { enabled in
                            if enabled { model.upgradeServers.insert(name) } else { model.upgradeServers.remove(name) }
                        })).toggleStyle(.checkbox)
                    }
                }
                Button("Save server selection", action: model.saveUpgradeTargets)
            }
            Text("Targets: " + (model.cliTargets.isEmpty ? "None selected" : model.cliTargets.joined(separator: ", ")))
                .font(.callout.weight(.medium)).textSelection(.enabled)
            Text("SSH targets are checked through their login shell. Codex QR-paired Remote connections cannot be reused by this app. To manage a paired device without SSH, run the maintenance CLI on that device through its Codex Remote chat.")
                .font(.caption).foregroundStyle(.secondary)
            Table(model.cliRows) {
                TableColumn("Host", value: \.host).width(130)
                TableColumn("CLI", value: \.tool).width(75)
                TableColumn("Installed", value: \.installed).width(105)
                TableColumn("Latest", value: \.latest).width(105)
                TableColumn("Status") { row in Text(row.state).help(row.detail) }.width(min: 130)
                TableColumn("Details", value: \.detail).width(min: 140)
            }.frame(minHeight: 220)
            Text(model.cliSummary).font(.callout).textSelection(.enabled)
            if model.status == "Needs attention" {
                Text(model.report).font(.caption).foregroundStyle(.orange).lineLimit(5).textSelection(.enabled)
            }
            HStack {
                Button("Close") { model.showCLIs = false }.disabled(model.busy)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("Check versions", action: model.versions).disabled(model.busy)
                Button("Upgrade selected CLIs", action: model.upgrade).disabled(model.busy || model.cliTargets.isEmpty)
            }
        }.padding(24).frame(width: 980, height: 620)
        .alert("Upgrade CLIs on these targets?", isPresented: $model.cliConfirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Upgrade", action: model.applyCLIUpgrade)
        } message: { Text(model.cliConfirmationText) }
    }
}

private struct SourceSettingsView: View {
    @ObservedObject var model: MaintenanceModel
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Sources & SSH").font(.title2.weight(.semibold))
            if model.status == "Needs attention" {
                Text(model.report).font(.caption).foregroundStyle(.orange).lineLimit(5).textSelection(.enabled)
            }
            Toggle("Discover hosts automatically from SSH and existing Agent Speed settings", isOn: $model.autoHosts)
            Text("Selected SSH aliases (one per line)").font(.callout.weight(.medium))
            TextEditor(text: $model.hostDraft).font(.system(.body, design: .monospaced))
                .frame(height: 100).border(Color.secondary.opacity(0.3)).disabled(model.autoHosts)
            HStack {
                Text("Detected: " + (model.discoveredHosts.isEmpty ? "No literal aliases in ~/.ssh/config" : model.discoveredHosts.joined(separator: ", ")))
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Use detected hosts") {
                    model.hostDraft = model.discoveredHosts.joined(separator: "\n")
                    model.autoHosts = false
                }
            }
            Text("Local data homes").font(.callout.weight(.medium))
            HStack {
                TextField("Codex home (default: ~/.codex)", text: $model.codexDraft).textFieldStyle(.roundedBorder)
                Button("Choose") { model.chooseDirectory(codex: true) }
            }
            HStack {
                TextField("Claude home (default: ~/.claude)", text: $model.claudeDraft).textFieldStyle(.roundedBorder)
                Button("Choose") { model.chooseDirectory(codex: false) }
            }
            Text("Leave a data home empty to use its default. Environment overrides take precedence. SSH credentials stay in your existing SSH configuration.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Cancel") { model.showSources = false }
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("Save", action: model.saveSources).buttonStyle(.borderedProminent).disabled(model.busy)
            }
        }.padding(24).frame(width: 640)
    }
}

@main
struct AgentMaintenanceApp: App {
    init() {
        if CommandLine.arguments.contains("--self-check") {
            do {
                let configuration = try CLI.execute(["config"])
                let diagnostics = configuration["local_diagnostics"] as? [String: Any] ?? [:]
                let targets = configuration["upgrade_hosts"] as? [String] ?? []
                var upgradeArguments: [String] = []
                if !targets.isEmpty { upgradeArguments = try CLIUpgradeRequest(hosts: targets, tool: "all").arguments(apply: false) }
                let report: [String: Any] = [
                    "hosts": configuration["hosts"] as? [String] ?? [],
                    "upgrade_hosts": targets,
                    "upgrade_arguments": upgradeArguments,
                    "execution_host": diagnostics["hostname"] as? String ?? "",
                    "python": configuration["python"] as? String ?? "",
                    "codex": diagnostics["codex"] as? [String: Any] ?? [:],
                    "claude": diagnostics["claude"] as? [String: Any] ?? [:],
                    "iconBundled": Bundle.main.url(forResource: "AgentMaintenance", withExtension: "icns") != nil,
                ]
                let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
                print(String(decoding: data, as: UTF8.self))
                Darwin.exit(0)
            } catch {
                FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
                Darwin.exit(1)
            }
        }
    }
    var body: some Scene {
        WindowGroup("Agent Maintenance") { MaintenanceView() }
            .defaultSize(width: 1180, height: 780)
    }
}
