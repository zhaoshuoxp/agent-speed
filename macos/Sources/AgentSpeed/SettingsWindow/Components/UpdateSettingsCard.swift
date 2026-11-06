import AgentSpeedCore
import SwiftUI

/// Settings › About update controls (UPDATES section 4): status, last check, Check for Updates and the
/// automatic-check switch.
struct UpdateSettingsCard: View {
    let model: AppModel
    @Environment(\.colorSchemeContrast) private var contrast

    var body: some View {
        @Bindable var settings = model.settings
        let l10n = model.l10n
        let shape = RoundedRectangle(cornerRadius: 12, style: .continuous)
        VStack(spacing: 0) {
            if !model.updates.updatesAllowed {
                HStack(alignment: .top, spacing: Spacing.s) {
                    Image(systemName: "lock.shield.fill").foregroundStyle(.blue)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("SSH Usage custom build").fontWeight(.medium)
                        Footnote("Upstream update checks, downloads, and installation are disabled to preserve the custom SSH controls. Updates are installed from a rebuilt custom package.")
                    }
                    Spacer(minLength: 0)
                }
                .padding(Spacing.m)
            } else {
            UpdateStatusRow(model: model)
                .padding(Spacing.m)
            Hairline()
                .padding(.horizontal, Spacing.m)
            HStack(alignment: .center, spacing: Spacing.s) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(l10n("update.auto"))
                    Footnote(settings.networkEnabled ? l10n("update.auto.hint") : l10n("settings.network.offHint"))
                }
                .accessibilityHidden(true)
                Spacer(minLength: Spacing.xs)
                Toggle(l10n("update.auto"), isOn: $settings.autoCheckUpdates)
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .accessibilityHint(l10n("update.auto.hint"))
            }
            .padding(Spacing.m)
            // With the network master switch off, no check runs (NETWORK 2).
            .disabled(!settings.networkEnabled)
            }
        }
        .background(Color.primary.opacity(0.04), in: shape)
        .overlay {
            shape.strokeBorder(Color.primary.opacity(contrast == .increased ? 0.3 : 0.08), lineWidth: 1)
        }
    }
}

private struct UpdateStatusRow: View {
    let model: AppModel

    var body: some View {
        let l10n = model.l10n
        let updates = model.updates
        let state = UpdateStatusState(updates)
        HStack(alignment: .center, spacing: Spacing.s) {
            UpdateStatusBadge(state: state)
            VStack(alignment: .leading, spacing: 2) {
                Text(title(l10n, state: state))
                    .font(.callout.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
                    .contentTransition(.opacity)
                TimelineView(.periodic(from: .now, by: 30)) { context in
                    if let detail = detail(l10n, now: context.date) {
                        Text(detail)
                            .font(.subheadline)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: Spacing.xs)
            if let release = updates.availableRelease {
                Button(l10n("update.menu", release.version.description)) {
                    model.commands?.showUpdate()
                }
                .buttonStyle(.borderedProminent)
            } else {
                Button(l10n("update.check")) {
                    updates.startCheck(manual: true)
                }
                .disabled(updates.isBusy || !model.settings.networkEnabled)
                .help(model.settings.networkEnabled ? "" : l10n("settings.network.offHint"))
            }
        }
        .animation(Motion.fade, value: state)
    }

    private func title(_ l10n: Localizer, state: UpdateStatusState) -> String {
        let updates = model.updates
        switch updates.installation {
        case .downloading(let fraction):
            return l10n("update.downloading", l10n("mac.settings.percentValue", Int((fraction * 100).rounded(.down))))
        case .verifying:
            return l10n("update.verifying")
        case .installing, .relaunching:
            return l10n("update.installing")
        case .failed(let failure):
            return l10n.failureText(failure)
        case .idle:
            break
        }
        switch updates.status {
        case .idle: return l10n("update.current", updates.currentVersion)
        case .checking: return l10n("update.checking")
        case .upToDate: return l10n("update.upToDate")
        case .available(let release): return l10n("update.available.title", release.version.description)
        case .failed(let failure): return l10n.failureText(failure)
        }
    }

    /// "Last checked 5 minutes ago", led by the current version while an update is on offer.
    private func detail(_ l10n: Localizer, now: Date) -> String? {
        let updates = model.updates
        var parts: [String] = []
        if updates.availableRelease != nil { parts.append(l10n("update.current", updates.currentVersion)) }
        if let checked = updates.lastChecked {
            let when = now.timeIntervalSince(checked) < 60
                ? l10n("time.justNow")
                : checked.formatted(Date.RelativeFormatStyle(presentation: .named, unitsStyle: .wide).locale(l10n.locale))
            parts.append(l10n("update.lastChecked", when))
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

/// The badge's look for each state.
private enum UpdateStatusState: Equatable {
    case idle, busy, upToDate, available, failed

    @MainActor
    init(_ updates: UpdateController) {
        if updates.installation.isActive || updates.isChecking {
            self = .busy
        } else if case .failed = updates.installation {
            self = .failed
        } else {
            switch updates.status {
            case .idle: self = .idle
            case .checking: self = .busy
            case .upToDate: self = .upToDate
            case .available: self = .available
            case .failed: self = .failed
            }
        }
    }
}

private struct UpdateStatusBadge: View {
    let state: UpdateStatusState

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 7, style: .continuous)
        ZStack {
            shape.fill(fill)
            if state == .busy {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.8)
            } else {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(.white)
                    .contentTransition(.symbolEffect(.replace))
            }
        }
        .frame(width: 26, height: 26)
        .accessibilityHidden(true)
    }

    private var symbol: String {
        switch state {
        case .idle, .busy: "arrow.triangle.2.circlepath"
        case .upToDate: "checkmark"
        case .available: "arrow.down"
        case .failed: "exclamationmark"
        }
    }

    private var fill: Color {
        switch state {
        case .idle: Color.secondary.opacity(0.55)
        case .busy: Color.primary.opacity(0.08)
        case .upToDate: Color.green
        case .available: Color.accentColor
        case .failed: Color.orange
        }
    }
}
