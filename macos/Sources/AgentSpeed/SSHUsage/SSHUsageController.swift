import AppKit
import Foundation
import Observation

struct SSHHostCache: Decodable, Identifiable, Sendable {
    let host: String
    let files: Int
    let bytes: Int64
    let diskBytes: Int64
    let codexFiles: Int
    let codexBytes: Int64
    let claudeFiles: Int
    let claudeBytes: Int64
    let state: String
    let lastSuccess: String?
    let error: String?
    var id: String { host }
}

struct SSHUsageSnapshot: Decodable, Sendable {
    let configured: Bool
    let enabled: Bool
    let serviceLoaded: Bool
    let state: String
    let lastSync: String?
    let intervalSeconds: Int
    let files: Int
    let bytes: Int64
    let diskBytes: Int64
    let hosts: [SSHHostCache]
    let message: String?

    static let empty = SSHUsageSnapshot(
        configured: false, enabled: false, serviceLoaded: false, state: "unconfigured", lastSync: nil,
        intervalSeconds: 30, files: 0, bytes: 0, diskBytes: 0, hosts: [], message: nil
    )
}

/// Owns the local SSH sync service and its downloaded cache. Usage history remains owned by HistoryService.
@MainActor
@Observable
final class SSHUsageController {
    private(set) var snapshot = SSHUsageSnapshot.empty
    private(set) var isBusy = false
    private(set) var isRefreshing = false
    private(set) var error: String?
    private(set) var message: String?
    @ObservationIgnored private let backend: SSHUsageBackend

    init(directory: URL) {
        backend = SSHUsageBackend(directory: directory)
    }

    func refresh() async {
        guard !isBusy, !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let value = try await backend.request(["--info"])
            if !isBusy { snapshot = value }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func setEnabled(_ enabled: Bool) {
        perform(["--service", enabled ? "enable" : "disable"])
    }

    func syncNow() { perform(["--sync"]) }

    func clearCache(host: String?, days: Int) {
        var arguments = ["--clear", "--days", String(days)]
        if let host { arguments += ["--host", host] }
        perform(arguments)
    }

    private func perform(_ arguments: [String]) {
        guard !isBusy else { return }
        isBusy = true
        error = nil
        message = nil
        Task {
            defer { isBusy = false }
            do {
                snapshot = try await backend.request(arguments)
                message = snapshot.message
            } catch {
                self.error = error.localizedDescription
                if let actual = try? await backend.request(["--info"]) { snapshot = actual }
            }
        }
    }

    static func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func relativeDate(_ value: String?) -> String {
        guard let value, let date = ISO8601DateFormatter().date(from: value) else { return "No completed sync" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}

private struct SSHUsageBackend: Sendable {
    let directory: URL

    func request(_ arguments: [String]) async throws -> SSHUsageSnapshot {
        try await Task.detached(priority: .utility) {
            try execute(arguments)
        }.value
    }

    private func execute(_ arguments: [String]) throws -> SSHUsageSnapshot {
        let files = FileManager.default
        let config = directory.appendingPathComponent("config.json")
        guard files.fileExists(atPath: config.path) else { return .empty }
        let helper = directory.appendingPathComponent("sync_usage.py")
        if let bundled = Bundle.main.url(forResource: "sync_usage", withExtension: "py", subdirectory: "SSHUsage") {
            let body = try Data(contentsOf: bundled)
            if (try? Data(contentsOf: helper)) != body {
                try body.write(to: helper, options: .atomic)
                try files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: helper.path)
            }
        }
        guard files.fileExists(atPath: helper.path) else {
            throw SSHUsageError("The bundled SSH sync helper could not be installed.")
        }
        let candidates = interpreterCandidates()
        guard let python = candidates.first(where: { files.isExecutableFile(atPath: $0) }) else {
            throw SSHUsageError("Python 3 is required by the configured SSH synchronizer.")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [helper.path, "--config", config.path] + arguments
        process.currentDirectoryURL = directory
        process.standardInput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        let box = SSHUsageProcess(process: process)
        let timeout = DispatchWorkItem { if box.process.isRunning { box.process.terminate() } }
        try process.run()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1800, execute: timeout)
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        timeout.cancel()
        if process.terminationStatus != 0 {
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw SSHUsageError(object?["error"] as? String ?? "The SSH sync operation failed. Refresh and try again.")
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(SSHUsageSnapshot.self, from: data)
    }

    private func interpreterCandidates() -> [String] {
        let agent = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/io.github.zoefix.agentspeed.ssh-usage.plist")
        var paths: [String] = []
        if let data = try? Data(contentsOf: agent),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let first = (plist["ProgramArguments"] as? [String])?.first,
           URL(fileURLWithPath: first).lastPathComponent.hasPrefix("python3") {
            paths.append(first)
        }
        paths += ["/opt/homebrew/bin/python3", "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/usr/bin/python3"]
        return paths
    }
}

private struct SSHUsageProcess: @unchecked Sendable { let process: Process }

private struct SSHUsageError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}
