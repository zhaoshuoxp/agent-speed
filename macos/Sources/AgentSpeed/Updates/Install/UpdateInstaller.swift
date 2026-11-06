import AgentSpeedCore
import Foundation

/// The install pipeline of UPDATES section 5: download, verify, extract and validate, replace. The caller relaunches.
struct UpdateInstaller: Sendable {
    enum Step: Sendable, Equatable {
        case downloading(Double)
        case verifying
        case installing
    }

    let configuration: UpdateConfiguration
    /// The running bundle (`Bundle.main.bundleURL`).
    let target: URL

    var replacer: BundleReplacer { BundleReplacer(target: target) }

    /// Every temporary file lives in one private workspace that is deleted on success and on failure.
    @concurrent
    func install(_ release: Release, progress: @escaping @Sendable (Step) -> Void) async throws {
        guard configuration.updatesAllowed else { throw CustomBuildUpdateError() }
        guard target.pathExtension == "app" else { throw UpdateFailure.notBundled }
        try replacer.checkWritable()
        let (archiveAsset, sumsAsset) = try assets(of: release)

        let workspace = try UpdateWorkspace.create()
        defer { workspace.remove() }
        let http = UpdateHTTP(configuration: configuration)

        progress(.downloading(0))
        let expected = try await expectedDigest(of: archiveAsset, listedIn: sumsAsset, http: http)
        let archive = workspace.file(archiveAsset.name)
        do {
            try await http.download(
                http.assetRequest(archiveAsset.downloadURL),
                to: archive,
                expectedSize: archiveAsset.size
            ) { fraction in
                progress(.downloading(fraction))
            }
        } catch {
            throw Self.downloadFailure(error)
        }

        progress(.verifying)
        guard ChecksumList.matches(expected, try FileDigest.sha256(of: archive)) else {
            throw UpdateFailure.checksum
        }
        let app = try await PackageValidator(expectedVersion: release.version).extract(archive, in: workspace)

        progress(.installing)
        try replacer.replace(with: app)
    }

    private func assets(of release: Release) throws -> (archive: ReleaseAsset, sums: ReleaseAsset) {
        guard let archive = release.asset(named: UpdateConfiguration.assetName),
              let sums = release.asset(named: UpdateConfiguration.checksumAssetName),
              (1...UpdateConfiguration.maxAssetSize).contains(archive.size),
              (1...Int64(ChecksumList.maxBytes)).contains(sums.size),
              configuration.policy.permits(archive.downloadURL),
              configuration.policy.permits(sums.downloadURL) else {
            throw UpdateFailure.invalidPackage
        }
        return (archive, sums)
    }

    private func expectedDigest(of archive: ReleaseAsset, listedIn sums: ReleaseAsset, http: UpdateHTTP) async throws -> String {
        let data: Data
        do {
            data = try await http.data(for: http.assetRequest(sums.downloadURL), limit: sums.size, expectedSize: sums.size)
        } catch {
            throw Self.downloadFailure(error)
        }
        guard let text = String(data: data, encoding: .utf8),
              let digest = ChecksumList.digest(for: archive.name, in: text) else {
            throw UpdateFailure.checksum
        }
        return digest
    }

    private static func downloadFailure(_ error: any Error) -> UpdateFailure {
        switch error {
        case let failure as UpdateFailure: failure
        case HTTPTransferError.tooLarge, HTTPTransferError.sizeMismatch: .checksum
        case HTTPTransferError.disallowedURL: .invalidPackage
        case HTTPTransferError.write: .installFailed
        default: .network
        }
    }
}
