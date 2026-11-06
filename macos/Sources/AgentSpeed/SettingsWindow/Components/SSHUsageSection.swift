import SwiftUI

/// Settings > Sources: background SSH synchronization and local cache management.
struct SSHUsageSection: View {
    let model: AppModel
    @State private var cleanupDays = 30
    @State private var cleanupHost: String?
    @State private var confirmsCleanup = false

    private var controller: SSHUsageController { model.sshUsage }
    private var snapshot: SSHUsageSnapshot { controller.snapshot }

    var body: some View {
        Section {
            if snapshot.configured {
                SettingToggle(
                    title: "Background SSH sync",
                    subtitle: "Sync every 30 seconds, including while Agent Speed is closed. Starts at login.",
                    isOn: Binding(get: { snapshot.enabled }, set: { controller.setEnabled($0) })
                )
                .disabled(controller.isBusy)
                controls
                LabeledContent("Downloaded logs") {
                    VStack(alignment: .trailing, spacing: 3) {
                        ValueText("\(SSHUsageController.size(snapshot.bytes)) in \(snapshot.files) files")
                        Footnote("\(SSHUsageController.size(snapshot.diskBytes)) on disk")
                    }
                }
                Picker("Cleanup age", selection: $cleanupDays) {
                    Text("Older than 7 days").tag(7)
                    Text("Older than 30 days").tag(30)
                    Text("Older than 90 days").tag(90)
                    Text("All downloaded logs").tag(0)
                }
                .disabled(controller.isBusy)
                ForEach(snapshot.hosts) { host in
                    hostRow(host)
                }
                HStack {
                    Button("Clear all server caches…", role: .destructive) {
                        cleanupHost = nil
                        confirmsCleanup = true
                    }
                    .disabled(snapshot.files == 0 || controller.isBusy)
                    Spacer()
                    Text("\(snapshot.hosts.count) SSH servers").foregroundStyle(.secondary)
                }
            } else {
                Footnote("SSH usage sync has not been configured on this Mac.")
            }
            if let error = controller.error {
                Notice(text: error, symbol: "exclamationmark.triangle", tint: .orange)
            }
            if let message = controller.message {
                Notice(text: message, symbol: "checkmark.circle", tint: .green)
            }
        } header: {
            Text("SSH servers")
        } footer: {
            SectionFootnote("Cleanup keeps daily usage history and the original server logs. Cleared log versions stay excluded until the server changes them. Files awaiting history indexing are kept.")
        }
        .alert("Clear downloaded logs?", isPresented: $confirmsCleanup) {
            Button("Clear local cache", role: .destructive) {
                model.historyService?.refresh()
                controller.clearCache(host: cleanupHost, days: cleanupDays)
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(cleanupDescription)
        }
        .task {
            await controller.refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                await controller.refresh()
            }
        }
    }

    private var controls: some View {
        HStack(spacing: Spacing.s) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if controller.isBusy || snapshot.state == "syncing" {
                        ProgressView().controlSize(.mini)
                    }
                    Text(statusTitle)
                }
                Footnote("Last sync: \(SSHUsageController.relativeDate(snapshot.lastSync))")
            }
            Spacer(minLength: Spacing.xs)
            Button("Sync now") { controller.syncNow() }
                .disabled(controller.isBusy)
            Button("Refresh sizes") { Task { await controller.refresh() } }
                .disabled(controller.isBusy || controller.isRefreshing)
        }
    }

    private func hostRow(_ host: SSHHostCache) -> some View {
        HStack(alignment: .center, spacing: Spacing.s) {
            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: host.host).fontWeight(.medium)
                Footnote("Codex: \(SSHUsageController.size(host.codexBytes)) · Claude: \(SSHUsageController.size(host.claudeBytes))")
                if let error = host.error {
                    Text(verbatim: error).font(.caption).foregroundStyle(.orange).lineLimit(2).help(error)
                }
            }
            Spacer(minLength: Spacing.xs)
            VStack(alignment: .trailing, spacing: 3) {
                ValueText(SSHUsageController.size(host.bytes))
                Footnote("\(host.files) files")
            }
            Button("Clear…") {
                cleanupHost = host.host
                confirmsCleanup = true
            }
            .accessibilityLabel("Clear downloaded logs for \(host.host)")
            .disabled(host.files == 0 || controller.isBusy)
        }
    }

    private var statusTitle: String {
        if controller.isBusy { return "Updating SSH sync…" }
        if !snapshot.enabled { return "Paused" }
        if !snapshot.serviceLoaded { return "Background service is stopped" }
        if snapshot.state == "syncing" { return "Syncing…" }
        if snapshot.hosts.contains(where: { $0.state == "error" }) { return "Some servers need attention" }
        return "Background sync is on"
    }

    private var cleanupDescription: String {
        let scope = cleanupHost ?? "all SSH servers"
        let age = cleanupDays == 0 ? "all downloaded logs" : "downloaded logs older than \(cleanupDays) days"
        return "Clear \(age) for \(scope)? Daily usage history and original server logs are kept. Unchanged cleared logs will stay excluded from future syncs."
    }
}
