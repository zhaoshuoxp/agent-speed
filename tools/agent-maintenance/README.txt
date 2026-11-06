Agent Maintenance 0.1.3

DESKTOP APP
The separate Agent Maintenance macOS app uses this same CLI. It lists local and SSH hosts, filters conversations by project/tool/archive state, backs up selected conversations or a project, previews archive/unarchive/delete and log/temp cleanup, verifies and restores standalone backups, and checks or applies CLI upgrades. Conversation changes and cleanup show a reviewed plan before confirmation. CLI upgrades require explicit confirmation. The main operation log shows live command output and timestamped results. Configuration JSON is available through Diagnostics. Build a universal app with desktop/build-app.sh OUTPUT_DIRECTORY using a compatible SDK or complete Xcode installation. Python 3 must be available on the Mac and remote hosts. No traffic monitor is installed.

Completed operation summaries are saved to ~/Library/Application Support/AgentMaintenance/operations.log, with a bounded history. Clear records removes this history and the current display. Live command output is held in memory. The Remote CLI maintenance log is separate, is never saved by the app, and clears whenever its panel closes. The backend also supports --progress to stream progress to stderr while preserving JSON or backup bytes on stdout.

LEGACY ENTRIES
install_legacy.py --scripts-dir PATH installs delegates for codex_archives.py and claude_sessions.py while retaining timestamped originals. The Claude delegate preserves --archived and --filter. The Codex delegate starts in the archived-conversation view. legacy_codex_update_server.sh can replace the previous updater beside the installed agent-maintenance launcher; positional SSH aliases still work. It explicitly requests native Codex and Claude upgrades when run. Installing the delegate itself performs no upgrade.

Inspect and maintain local and remote Codex and Claude conversations with Python 3.9+ and OpenSSH. Remote commands stream a small Python engine over SSH; no permanent remote daemon is required. All inventory operations are read-only. Network traffic is not monitored.

INSTALL
  python3 install.py
  agent-maintenance hosts
  agent-maintenance inspect --host all
  agent-maintenance interactive

Hosts come from ~/.config/agent-maintenance/config.json (a JSON object with a hosts array of SSH aliases), then the existing Agent Speed SSHUsage configuration on macOS. AGENT_MAINTENANCE_HOSTS can supply comma-separated aliases. Without explicit configuration, literal aliases are discovered from ~/.ssh/config and its Include files. Wildcard and negated Host patterns are excluded. Use Sources & SSH in the desktop app to select aliases and local data homes. Explicit --host values always work; repeat --host to select several machines.

BACK UP A PROJECT OR CONVERSATION
  agent-maintenance projects --host all
  agent-maintenance sessions --host my-server --tool codex --archived all
  agent-maintenance backup --host my-server --project /absolute/project/path --output project.zip
  agent-maintenance backup --host my-server --session CONVERSATION_UUID --output conversation.zip
  agent-maintenance verify conversation.zip
  agent-maintenance restore-files conversation.zip --output restored-files

Project paths select that project and nested working directories. A project name or ID selects an exact project. Child and guardian sessions are included with their parent, even when their relationship exists only in transcript metadata. Recent or active conversations are protected; retry after they have been idle for at least five minutes.

Backups stream compressed ZIP64 directly to the destination computer. They contain original transcript bytes, child sessions, associated attachments and generated images, per-conversation database row exports, a readable HTML export, and a SHA-256 manifest. Inline images and tool output remain in the originals. Each member is verified before the final ZIP is committed. Failed partial files are retained. Backup ZIPs contain private conversation data; keep them in your chosen private storage.

restore-files extracts to a new standalone directory. Original formats and row exports are preserved, but automatic import into a native desktop sidebar is not implemented. Use unarchive for conversations that still exist in the original native archive.

ARCHIVE, RESTORE ARCHIVED, OR DELETE
  agent-maintenance plan archive --host my-server --session CONVERSATION_UUID --output archive-plan.json
  agent-maintenance apply archive-plan.json --confirm FULL_PLAN_ID
  agent-maintenance plan unarchive --host my-server --session CONVERSATION_UUID --output restore-plan.json
  agent-maintenance plan delete --host my-server --project /absolute/project/path --output delete-plan.json

Review the plan before running the printed apply command. Archive and delete use manual backups: neither asks for nor verifies a backup file. The desktop app shows the selected host, conversations, descendants, and size before confirmation. Changed files, active conversations, and altered or expired plans are refused. Plans expire after 24 hours. Broad selection requires explicit --all. No files are removed during a plan. The legacy --backup argument on plan is accepted for compatibility and does not impose backup verification.

Codex operations use the installed CLI's native archive, unarchive, and delete commands. On macOS, CLI discovery also checks standard installation paths and desktop app bundles when Finder provides a minimal PATH. Deletion of a metadata-only record still invokes the native CLI; success requires the record to disappear from inventory. Claude file archive moves complete conversation bundles to ~/.claude-archive/projects, and unarchive moves them back. The app labels this as File archive when Claude is selected. This is a maintenance feature, separate from Claude Desktop's native Archive. The checked server CLI and published SDK do not expose an equivalent archive/unarchive API for these terminal sessions. Archives retain transcript bytes; deleting releases those transcript files. SQLite databases may retain allocated pages, and package runtimes remain installed. Agent Speed daily usage history is independent of source conversation deletion. Its synchronizer removes indexed orphan hard links when the local original has gone.

Scanning and backups work without an installed Codex or Claude CLI. Codex native archive/unarchive/delete require Codex on the host holding the files, including a supported desktop-bundled CLI on macOS. A local app can manage remote sessions without locally installed CLIs. Missing CLI rows identify the target host; upgrades do not perform first-time installation.

LOGS AND TEMPORARY FILES
  agent-maintenance plan cleanup --host my-server --category logs --category temp --older-than 30 --output cleanup-plan.json
  agent-maintenance apply cleanup-plan.json --confirm FULL_PLAN_ID

Cleanup only considers known log/cache/temp directories and rechecks every file against the current whitelist. It excludes conversation transcripts, databases, credentials, configuration, and installed CLI runtimes. Use inspect to distinguish large sessions, history databases, generated media, logs, and installed runtime releases. Database compaction and runtime release deletion are not performed.

CLI UPGRADES
  agent-maintenance upgrade
  agent-maintenance upgrade --tool codex --apply
  agent-maintenance upgrade --tool claude --apply
  agent-maintenance config --upgrade-hosts thor,loki,freya,bordeaux,champagne
  agent-maintenance upgrade --host local --tool codex

The desktop Remote CLI maintenance panel has independent server checkboxes and a Codex/Claude selector. It defaults to selected SSH servers, regardless of the conversation host, and shows installed/latest versions and per-host errors. A live log shows the native updater's stdout and stderr as they arrive. The selected servers can be saved. This computer and the conversation host are explicit alternate targets.

Without --host, upgrade targets the saved upgrade_hosts list. The bundled profile prefers thor, loki, freya, bordeaux, and champagne among discovered aliases; otherwise configured remote hosts are selected. An explicitly empty list requires choosing targets. Use --host local to check or update the computer running the command.

SSH runs use a login shell and ~/.local/bin, following the legacy server updater. Checks compare semantic versions against official release channels. Applying delegates to codex update or claude update, skips installed versions at or above the channel version, and verifies the resulting version. Updates run one server at a time, including when server homes share an installation. Missing CLIs are reported without installation. Desktop-bundled CLIs are updated with their owning desktop app. No upgrade is performed without --apply. Every host result is reported; failures produce a nonzero exit code. --json is available for desktop clients and automation.

CODEX REMOTE PAIRING
Codex desktop's QR-paired Remote channel is not integrated into this independent app. Its paired device is not automatically an SSH target. Install the maintenance CLI on that device and execute it from a Codex Remote chat on the device; use --host local to manage that device's files. In the desktop maintenance app, App computer identifies the computer running the app, and Record host identifies the selected data source. Direct QR-channel management would require a separate supported transport integration.

VALIDATION
  python3 -m unittest -v test_maintenance.py
  python3 validate_native_codex.py

The native validation uses a temporary CODEX_HOME, creates fixture conversations without model calls, and exercises archive, unarchive, and delete without backup prerequisites. Normal tests cover backup bytes/media/descendants, restoration, changed-file rejection, cleanup scope, live stdout/stderr delivery before command completion, process timeouts, and streaming CLI plan/apply. The packaged app check also verifies live pipe output and saving/clearing operation records with isolated fixtures. Real server checks are read-only. No production deletion is part of validation.

SOURCE DIAGNOSTICS
  agent-maintenance diagnose --host all
  agent-maintenance config
  agent-maintenance config --hosts thor,loki
  agent-maintenance config --auto-hosts
  agent-maintenance config --codex-home ~/.codex

Diagnostics show actual data directories, database paths, record counts, and scan errors without conversation content. A guardian-only Codex store is no longer hidden: orphan subagent roots are visible by default, and Include subagents reveals every record. The desktop table labels subagents separately. Environment data-home overrides take precedence over configured paths.

The macOS distribution installer and app share version-aware Python discovery. Python 3.9.6 from macOS developer tools is supported and tested; no separate Python upgrade is needed when that interpreter is available.
