# Custom SSH usage build

This fork adds SSH transcript synchronization controls to macOS Settings > Sources. Agent Speed indexes mirrored Codex and Claude transcripts with its existing usage engine. It does not need a remote Agent Speed application or traffic monitor.

The Sources panel controls the existing per-user background service, runs a sync, refreshes cache sizes, and clears cached server transcripts by host and age. Configuration and credentials remain on the user's machine under `~/Library/Application Support/AgentSpeed/SSHUsage`. The bundled Python helper updates that installation when the panel is used. SSH uses existing host aliases, verified host keys, and no agent forwarding.

Cache cleanup only removes local server copies already indexed at exactly the same file version by `history.sqlite`. Remote transcripts and local original transcripts remain intact. The daily usage history stays in the native database. A tombstone prevents unchanged cleared files from being downloaded again; changed remote transcripts are eligible for synchronization. This is separate from deleting a conversation on a server.

Local mirrors use hard links where possible. When another maintenance tool deletes an original transcript, synchronization removes its indexed orphan mirror to release the shared disk allocation. Unindexed mirrors remain protected.

## Upstream updates

All apps packaged by this fork carry `AgentSpeedCustomBuild=true`. The update controller and installer block upstream checks, downloads, and replacement, including manual actions and developer feed overrides. Settings > About identifies the custom build. Native data and preferences retain the upstream bundle identifier.

The `Sync upstream and rebuild custom app` GitHub Actions workflow checks daily at 14:17 UTC and can be started manually. When upstream changes, it merges `zoefix/agent-speed:main` into a local candidate, runs native tests and SSH cache safety tests, builds both universal signed apps, and executes the packaged update-protection check. It advances this fork's `main` only after those steps succeed. A conflicting merge or failed check leaves `main` unchanged. A concurrent change to `main` makes the normal push fail safely; no force push is used. Scheduled runs without upstream changes skip the build; a manual run always rebuilds.

Download `AgentSpeed-custom-macOS-<run>` from the successful workflow's artifacts, verify its `SHA256SUMS.txt`, quit Agent Speed, and replace the application. The artifact includes its exact source commit and remains available for 30 days. Existing application data stays in Application Support. GitHub may disable schedules after prolonged repository inactivity; enable or manually run the workflow from the Actions tab if that happens.

## Local build and validation

```sh
python3 -m unittest discover -s macos/Tests/SSHUsageHelperTests -v
swift test --package-path macos
macos/scripts/build-app.sh --custom-build
python3 macos/scripts/test-custom-build.py 'dist/Agent Speed.app'
```

Use a complete Xcode installation for the full Swift test suite. `AGENTSPEED_SDK_PATH` optionally selects a compatible SDK for command-line builds. This fork uses conventional `EnvironmentKey` declarations for five environment values so the app can also compile with an SDK without the SwiftUI `@Entry` macro plugin.

The packaged protection check starts before the application model, native databases, and SSH service. It uses isolated defaults and a nonexistent temporary install target. The upstream relaunch integration fixture explicitly permits updates only inside its isolated test bundles.

Upstream source and license credit remain unchanged. Custom source, tests, and workflows contain no user transcript backups, SSH keys, credentials, or live machine configuration.

## Conversation maintenance companion

`tools/agent-maintenance` contains the separate maintenance CLI and its tests. It inventories local and SSH conversation stores, streams verified project or conversation backups, plans archive/unarchive/delete operations, previews log and temporary-file cleanup, and delegates CLI upgrades to the native updater. See its `README.txt` for commands and restoration limits. It reads existing SSH host configuration at runtime and does not embed live server paths or configuration.
