import AgentSpeedCore
import AppKit
import Darwin

/// Exercises the packaged app without opening user databases or starting SSH synchronization.
@MainActor
enum CustomUpdateProtectionCheck {
    static func start() {
        Task {
            do {
                try await verify()
                print("{\"customBuild\":true,\"automaticChecksBlocked\":true,\"manualChecksBlocked\":true,\"installationBlocked\":true}")
                Darwin.exit(0)
            } catch {
                FileHandle.standardError.write(Data("Custom update protection failed: \(error)\n".utf8))
                Darwin.exit(1)
            }
        }
    }

    private static func verify() async throws {
        func require(_ condition: Bool, _ message: String) throws {
            if !condition { throw CheckError(message: message) }
        }
        try require(UpdateConfiguration.isCustomBuild, "Missing custom bundle marker")
        let configuration = UpdateConfiguration.current(environment: [
            UpdateConfiguration.feedOverrideVariable: "http://127.0.0.1:1/releases/latest",
        ])
        try require(!configuration.updatesAllowed, "A feed override enabled upstream updates")
        let suite = "io.github.zoefix.agentspeed.update-check.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { throw CheckError(message: "No isolated defaults") }
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = UpdateStore(defaults: defaults)
        let controller = UpdateController(configuration: configuration, store: store)
        try require(!controller.checksAutomatically, "Automatic checks enabled")
        await controller.check(manual: false)
        await controller.check(manual: true)
        try require(controller.status == .idle && store.lastCheck == nil, "A check contacted the feed")
        let release = Release(tag: "v999.0.0", version: SemanticVersion(major: 999, minor: 0, patch: 0))
        controller.preview(status: .available(release))
        try require(controller.availableRelease == nil, "A cached upstream release is visible")
        controller.install()
        try require(controller.installation == .idle, "The controller started installation")
        let target = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".app")
        let installer = UpdateInstaller(configuration: configuration, target: target)
        do {
            try await installer.install(release, progress: { _ in })
            throw CheckError(message: "The installer accepted an upstream release")
        } catch is CustomBuildUpdateError {
            // The guard runs before filesystem access, asset validation, or downloads.
        }
        try require(!FileManager.default.fileExists(atPath: target.path), "The installer touched its target")
    }

    private struct CheckError: Error {
        let message: String
    }
}
