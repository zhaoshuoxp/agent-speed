import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import URLError
import zipfile

import agent_maintenance as client
import host

PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
CLAUDE = "00000000-0000-4000-8000-000000000003"
ATTACHMENT = "00000000-0000-4000-8000-000000000004"


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.user_home = mock.patch.object(Path, "home", return_value=self.base)
        self.user_home.start()
        self.codex = self.base / ".codex"
        self.claude = self.base / ".claude"
        self.archived = self.base / ".claude-archive"
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex),
                                                        "CLAUDE_CONFIG_DIR": str(self.claude),
                                                        "CLAUDE_ARCHIVE_DIR": str(self.archived),
                                                        "AGENT_MAINTENANCE_HOSTS": "",
                                                        "AGENT_MAINTENANCE_CONFIG": str(self.base / "maintenance/config.json")})
        self.environment.start()
        self.age = time.time() - 45 * 86400
        self.project = self.base / "ExampleProject"
        self.project.mkdir()
        self.payloads = {}
        self.codex.mkdir()
        self.database = self.codex / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("create table threads(id text primary key,rollout_path text,created_at integer,updated_at integer,cwd text,title text,source text,archived integer,thread_source text)")
            for sid, parent in ((PARENT, None), (CHILD, PARENT)):
                path = self.codex / "sessions/2026/01/01" / ("rollout-" + sid + ".jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                meta = {"type": "session_meta", "payload": {"id": sid, "parent_thread_id": parent,
                          "cwd": str(self.project), "base_instructions": "example instructions"}}
                message = {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z",
                           "payload": {"type": "message", "role": "user", "content": [
                               {"type": "input_text", "text": "Review " + str(self.codex / "attachments" / ATTACHMENT / "image.png")}]}}
                path.write_text(json.dumps(meta) + "\n" + json.dumps(message) + "\n")
                os.utime(path, (self.age, self.age))
                self.payloads[sid] = path
                source = '{"subagent":{"other":"guardian"}}' if parent else "vscode"
                db.execute("insert into threads values(?,?,?,?,?,?,?,?,?)", (sid, str(path), int(self.age),
                           int(self.age), str(self.project), "Child" if parent else "Parent", source, 0,
                           "guardian_review" if parent else "user"))
            db.commit()
        asset = self.codex / "attachments" / ATTACHMENT / "image.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"example image bytes")
        image = self.codex / "generated_images" / PARENT / "generated.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"generated image bytes")
        self.claude_path = self.claude / "projects/example" / (CLAUDE + ".jsonl")
        self.claude_path.parent.mkdir(parents=True)
        self.claude_path.write_text(json.dumps({"type": "user", "cwd": str(self.project),
                                               "message": {"content": "Example conversation"}}) + "\n")
        os.utime(self.claude_path, (self.age, self.age))
        self.tool_output = self.claude_path.with_suffix("") / "tool-results/output.txt"
        self.tool_output.parent.mkdir(parents=True)
        self.tool_output.write_text("original tool output")
        self.history = self.codex / "thread_history_1.sqlite"
        with contextlib.closing(sqlite3.connect(self.history)) as db:
            db.execute("create table thread_items(thread_id text,item_json text)")
            db.execute("insert into thread_items values(?,?)", (PARENT, '{"record":"preserved history"}'))
            db.commit()

    def tearDown(self):
        self.user_home.stop()
        self.environment.stop()
        self.temp.cleanup()

    def make_backup(self, selector):
        path = self.base / ("backup-" + str(time.time_ns()) + ".zip")
        with path.open("wb") as handle:
            host.backup(selector, handle)
        return client.verify_backup(path)

    def test_parent_metadata_expands_guardian_children_without_database_edges(self):
        inventory = host.inventory()
        selected = host.selected_sessions(inventory, {"sessions": [PARENT]})
        self.assertEqual({row["id"] for row in selected}, {PARENT, CHILD})
        mains = host.selected_sessions(inventory, {"tool": "codex"}, expand=False)
        self.assertEqual([row["id"] for row in mains], [PARENT])

    def test_guardian_only_store_keeps_orphan_roots_visible(self):
        with contextlib.closing(sqlite3.connect(self.database)) as database:
            database.execute("update threads set source=?,thread_source='guardian_review' where id=?",
                             ('{"subagent":{"other":"guardian"}}', PARENT))
            database.commit()
        data = host.inventory()
        rows = host.selected_sessions(data, {"tool": "codex"}, expand=False)
        self.assertEqual([row["id"] for row in rows], [PARENT])
        self.assertTrue(rows[0]["is_subagent"])
        self.assertEqual(data["diagnostics"]["codex"]["total_records"], 2)
        self.assertEqual(data["diagnostics"]["codex"]["visible_records"], 1)
        every = host.selected_sessions(data, {"tool": "codex", "include_subagents": True}, expand=False)
        self.assertEqual({row["id"] for row in every}, {PARENT, CHILD})

    def test_ssh_alias_discovery_handles_includes_and_skips_patterns(self):
        ssh = self.base / ".ssh"
        (ssh / "config.d").mkdir(parents=True)
        (ssh / "config").write_text('Host thor loki\n  HostName example.test\nInclude config.d/*.conf\nHost * !private *.invalid\n')
        (ssh / "config.d/hosts.conf").write_text('Host=freya\nHost "bordeaux" champagne\nInclude config\n')
        self.assertEqual(client.configured_hosts(), ["local", "thor", "loki", "freya", "bordeaux", "champagne"])
        self.assertEqual(client.host_configuration()["host_source"], "SSH configuration")

    def test_explicit_host_configuration_can_select_local_only(self):
        path = client.configuration_path()
        path.parent.mkdir()
        path.write_text('{"hosts":[]}')
        self.assertEqual(client.configured_hosts(), ["local"])

    def test_source_configuration_preserves_existing_keys_and_backup(self):
        path = client.configuration_path()
        path.parent.mkdir()
        path.write_text('{"existing_option":"retained","hosts":["previous"]}')
        options = client.parser().parse_args(["config", "--hosts", "thor,loki", "--codex-home", str(self.codex)])
        result = client.configure(options)
        settings = json.loads(path.read_text())
        self.assertEqual(settings["existing_option"], "retained")
        self.assertEqual(result["hosts"], ["local", "thor", "loki"])
        self.assertEqual(settings["data_roots"]["codex"], str(self.codex))
        self.assertEqual(len(list(path.parent.glob("config.backup-*.json"))), 1)

    def test_projects_schema_without_name_does_not_hide_threads(self):
        with contextlib.closing(sqlite3.connect(self.database)) as database:
            database.execute("create table projects(id text,title text)")
            database.commit()
        rows, errors = host.scan_codex()
        self.assertEqual({row["id"] for row in rows}, {PARENT, CHILD})
        self.assertEqual(errors, [])

    def test_python_discovery_skips_an_incompatible_candidate(self):
        old = self.base / "old-python"
        old.write_text("#!/bin/sh\nexit 1\n")
        old.chmod(0o755)
        finder = Path(__file__).resolve().with_name("find-python.sh")
        found = subprocess.check_output(["/bin/bash", str(finder), str(old), sys.executable], text=True).strip()
        self.assertEqual(found, sys.executable)

    def test_backup_preserves_raw_media_children_database_rows_and_readable_export(self):
        proof = self.make_backup({"sessions": [PARENT], "tool": "codex"})
        self.assertEqual(proof["sessions"], 2)
        manifest = proof["manifest"]
        names = {item["name"] for item in manifest["files"]}
        self.assertIn("files/codex/attachments/" + ATTACHMENT + "/image.png", names)
        self.assertIn("files/codex/generated_images/" + PARENT + "/generated.png", names)
        self.assertTrue(any("thread_items.jsonl" in name for name in names))
        self.assertIn("readable/codex-" + PARENT + ".html", names)
        with zipfile.ZipFile(proof["path"]) as archive:
            entry = next(item for item in manifest["files"] if item.get("owner_id") == PARENT and item["kind"] == "transcript")
            self.assertEqual(archive.read(entry["name"]), self.payloads[PARENT].read_bytes())
        restored = self.base / "restored"
        client.restore_files(proof["path"], restored)
        self.assertEqual((restored / entry["name"]).read_bytes(), self.payloads[PARENT].read_bytes())

    def test_claude_archive_requires_backup_and_delete_uses_manual_backups(self):
        proof = self.make_backup({"sessions": [CLAUDE], "tool": "claude"})
        request = {"operation": "archive", "selector": {"sessions": [CLAUDE], "tool": "claude"},
                   "backup_sha256": proof["sha256"], "backup_path": proof["path"], "backup_proof": proof["manifest"]}
        plan = host.make_plan(request)
        result = host.apply_plan({"plan": plan, "confirm": plan["plan_id"], "verified_backup_sha256": proof["sha256"]})
        self.assertTrue(result["all_verified"])
        self.assertFalse(self.claude_path.exists())
        self.assertTrue((self.archived / "projects/example" / (CLAUDE + ".jsonl")).exists())
        plan = host.make_plan({"operation": "unarchive", "selector": {"sessions": [CLAUDE], "tool": "claude"}})
        self.assertTrue(host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})["all_verified"])
        plan = host.make_plan({"operation": "delete", "selector": {"sessions": [CLAUDE], "tool": "claude"}})
        self.assertIsNone(plan["backup_sha256"])
        self.assertTrue(host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})["all_verified"])
        self.assertFalse(self.claude_path.exists())
        self.assertFalse(self.tool_output.exists())
        self.assertTrue(Path(proof["path"]).exists())

    def test_stale_backup_and_unbacked_children_are_rejected(self):
        proof = self.make_backup({"sessions": [PARENT], "tool": "codex"})
        request = {"operation": "archive", "selector": {"sessions": [PARENT], "tool": "codex"},
                   "backup_sha256": proof["sha256"], "backup_path": proof["path"], "backup_proof": proof["manifest"]}
        request["backup_proof"] = {**proof["manifest"], "sessions": [row for row in proof["manifest"]["sessions"] if row["id"] != CHILD]}
        with self.assertRaises(ValueError):
            host.make_plan(request)
        request["backup_proof"] = proof["manifest"]
        with self.payloads[CHILD].open("a") as handle:
            handle.write('{"changed":true}\n')
        os.utime(self.payloads[CHILD], (self.age, self.age))
        with self.assertRaises(ValueError):
            host.make_plan(request)

    def test_changed_files_and_wrong_confirmation_cannot_apply(self):
        plan = host.make_plan({"operation": "delete", "selector": {"sessions": [CLAUDE]}})
        with self.assertRaises(ValueError):
            host.apply_plan({"plan": plan, "confirm": "wrong"})
        self.claude_path.write_text("changed")
        with self.assertRaises(RuntimeError):
            host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})
        self.assertTrue(self.claude_path.exists())

    def test_codex_metadata_only_record_is_deleted_through_native_cli(self):
        self.payloads[CHILD].unlink()
        plan = host.make_plan({"operation": "delete", "selector": {"sessions": [CHILD], "tool": "codex"}})
        self.assertEqual(plan["items"], [])

        def native_delete(arguments, **kwargs):
            self.assertEqual(arguments, ["/fixture/codex", "delete", CHILD, "--force"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.codex))
            with contextlib.closing(sqlite3.connect(self.database)) as database:
                database.execute("delete from threads where id=?", (CHILD,))
                database.commit()
            return subprocess.CompletedProcess(arguments, 0, stdout="Deleted", stderr="")

        with mock.patch.object(host, "find_cli", return_value="/fixture/codex"), \
                mock.patch.object(host.subprocess, "run", side_effect=native_delete) as run:
            result = host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})
        run.assert_called_once()
        self.assertTrue(result["all_verified"])

    def test_codex_delete_reports_remaining_metadata_as_needing_review(self):
        self.payloads[CHILD].unlink()
        plan = host.make_plan({"operation": "delete", "selector": {"sessions": [CHILD], "tool": "codex"}})
        response = subprocess.CompletedProcess([], 0, stdout="Deleted", stderr="")
        with mock.patch.object(host, "find_cli", return_value="/fixture/codex"), \
                mock.patch.object(host.subprocess, "run", return_value=response):
            result = host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})
        self.assertFalse(result["all_verified"])
        self.assertEqual(result["results"][0]["state"], "needs_review")

    def test_gui_cli_discovery_handles_a_minimal_path(self):
        binary = self.base / ".local/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        with mock.patch.object(host.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"AGENT_MAINTENANCE_CODEX_BIN": ""}):
            self.assertEqual(host.find_cli("codex"), str(binary))

    def test_recent_conversation_is_protected(self):
        os.utime(self.payloads[PARENT], None)
        with self.assertRaises(RuntimeError):
            host.backup({"sessions": [PARENT]}, io.BytesIO())

    def test_cleanup_does_not_touch_history_credentials_or_other_tool(self):
        log = self.codex / "log/old.log"
        log.parent.mkdir()
        log.write_text("old log")
        os.utime(log, (self.age, self.age))
        secret = self.codex / "auth.json"
        secret.write_text("credentials")
        plan = host.make_plan({"operation": "cleanup", "categories": ["logs"], "days": 30,
                               "selector": {"tool": "codex"}})
        self.assertEqual([item["path"] for item in plan["items"]], [str(log)])
        host.apply_plan({"plan": plan, "confirm": plan["plan_id"]})
        self.assertFalse(log.exists())
        self.assertTrue(secret.exists())
        self.assertTrue(self.payloads[PARENT].exists())
        self.assertTrue(self.history.exists())

    def test_backup_refuses_overwrite_and_restore_traversal(self):
        proof = self.make_backup({"sessions": [CLAUDE], "tool": "claude"})
        with self.assertRaises(ValueError):
            client.restore_files(proof["path"], self.base)
        with self.assertRaises(ValueError):
            client.safe_member("../../outside")
        with zipfile.ZipFile(proof["path"], "a") as archive:
            archive.writestr("extra.txt", b"unexpected")
        with self.assertRaises(ValueError):
            client.verify_backup(proof["path"])

    def test_cli_stream_backup_and_plan_apply(self):
        destination = self.base / "streamed.zip"
        receipt = client.stream_backup("local", {"sessions": [CLAUDE], "tool": "claude"}, destination)
        self.assertTrue(receipt["verified"])
        proof = client.verify_backup(destination)
        self.assertEqual(proof["sessions"], 1)
        plan_path = self.base / "delete-plan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            status = client.main(["plan", "delete", "--host", "local", "--tool", "claude", "--session", CLAUDE,
                                  "--output", str(plan_path), "--json"])
        self.assertEqual(status, 0)
        plan = json.loads(plan_path.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            status = client.main(["apply", str(plan_path), "--confirm", plan["plan"]["plan_id"], "--json"])
        self.assertEqual(status, 0)
        self.assertFalse(self.claude_path.exists())
        self.assertTrue(destination.exists())

    def test_upgrades_default_to_preferred_servers_and_can_explicitly_target_local(self):
        aliases = "thor,loki,freya,bordeaux,champagne,zoran"
        response = {"tools": []}
        with mock.patch.dict(os.environ, {"AGENT_MAINTENANCE_HOSTS": aliases}), \
                mock.patch.object(client, "invoke", return_value=response) as invoke, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.main(["upgrade", "--json"]), 0)
            self.assertEqual({call.args[0] for call in invoke.call_args_list}, set(aliases.split(",")) - {"zoran"})
            invoke.reset_mock()
            self.assertEqual(client.main(["upgrade", "--host", "local", "--tool", "codex", "--json"]), 0)
            self.assertEqual(invoke.call_args.args[:2], ("local", {"action": "versions", "tool": "codex",
                                                                "check_latest": True, "apply": False}))

    def test_empty_saved_upgrade_targets_do_not_fall_back_to_local(self):
        path = client.configuration_path()
        path.parent.mkdir()
        path.write_text('{"hosts":["thor"],"upgrade_hosts":[]}')
        output = io.StringIO()
        with mock.patch.object(client, "invoke") as invoke, contextlib.redirect_stdout(output):
            self.assertEqual(client.main(["upgrade", "--json"]), 1)
        invoke.assert_not_called()
        self.assertIn("No SSH upgrade targets", json.loads(output.getvalue())["error"])

    def test_upgrade_selection_is_saved_without_changing_source_hosts(self):
        path = client.configuration_path()
        path.parent.mkdir()
        path.write_text('{"hosts":["thor","loki"]}')
        args = client.parser().parse_args(["config", "--upgrade-hosts", "loki"])
        result = client.configure(args)
        self.assertEqual(result["hosts"], ["local", "thor", "loki"])
        self.assertEqual(result["upgrade_hosts"], ["loki"])
        self.assertEqual(json.loads(path.read_text())["upgrade_hosts"], ["loki"])

    def test_native_upgrade_skips_newer_versions_and_rechecks_updated_version(self):
        def response(value):
            return subprocess.CompletedProcess([], 0, stdout=value, stderr="")

        with mock.patch.object(host, "find_cli", return_value="/fixture/codex"), \
                mock.patch.object(host, "latest_version", return_value="0.166.0"), \
                mock.patch.object(host.subprocess, "run", return_value=response("codex-cli 0.167.0")) as run:
            result = host.tool_versions({"tool": "codex", "apply": True})["tools"][0]
        self.assertEqual(result["state"], "installed")
        self.assertFalse(result["update_available"])
        run.assert_called_once()
        sequence = [response("codex-cli 0.165.0"), response("Updated"), response("codex-cli 0.166.0")]
        with mock.patch.object(host, "find_cli", return_value="/fixture/codex"), \
                mock.patch.object(host, "latest_version", return_value="0.166.0"), \
                mock.patch.object(host.subprocess, "run", side_effect=sequence) as run:
            result = host.tool_versions({"tool": "codex", "apply": True})["tools"][0]
        self.assertEqual(run.call_args_list[1].args[0], ["/fixture/codex", "update"])
        self.assertEqual(result["state"], "updated")
        self.assertEqual(result["installed"], "0.166.0")

    def test_bundled_codex_cli_is_found_but_not_self_updated(self):
        response = subprocess.CompletedProcess([], 0, stdout="codex-cli 0.165.0", stderr="")
        with mock.patch.object(host, "find_cli", return_value="/Applications/Codex.app/Contents/Resources/codex"), \
                mock.patch.object(host, "latest_version", return_value="0.166.0"), \
                mock.patch.object(host.subprocess, "run", return_value=response) as run:
            result = host.tool_versions({"tool": "codex", "apply": True})["tools"][0]
        self.assertEqual(result["state"], "app_managed")
        run.assert_called_once()

    def test_upgrade_error_is_reported_with_a_nonzero_exit_code(self):
        result = {"tools": [{"tool": "codex", "state": "error", "error": "Release channel unavailable"}]}
        output = io.StringIO()
        with mock.patch.object(client, "invoke", return_value=result), contextlib.redirect_stdout(output):
            status = client.main(["upgrade", "--host", "example", "--json"])
        self.assertEqual(status, 1)
        response = json.loads(output.getvalue())
        self.assertEqual(response["failures"], [{"host": "example", "tool": "codex", "error": "Release channel unavailable"}])

    def test_framework_python_certificate_failure_uses_verified_system_curl(self):
        error = URLError(ssl.SSLCertVerificationError("Missing Python CA bundle"))
        response = subprocess.CompletedProcess([], 0, stdout=b'{"tag_name":"rust-v0.166.0"}')
        with mock.patch.object(host, "urlopen", side_effect=error), mock.patch.object(host.subprocess, "run", return_value=response) as run:
            self.assertEqual(host.fetch_text("https://releases.openai.com/codex/channels/latest"), response.stdout.decode())
        arguments = run.call_args.args[0]
        self.assertIn("=https", arguments)
        self.assertIn("--max-filesize", arguments)
        self.assertNotIn("--insecure", arguments)
        self.assertNotIn("-k", arguments)

    def test_legacy_install_preserves_readonly_originals_and_replaces_entries(self):
        scripts = self.base / "scripts"
        scripts.mkdir()
        for name in ("codex_archives.py", "claude_sessions.py"):
            path = scripts / name
            path.write_text("original " + name)
            path.chmod(0o444)
        installer = Path(__file__).resolve().with_name("install_legacy.py")
        subprocess.run([sys.executable, str(installer), "--scripts-dir", str(scripts)], check=True, capture_output=True)
        backup = next(scripts.glob("agent-maintenance-originals-*"))
        for name in ("codex_archives.py", "claude_sessions.py"):
            self.assertEqual((backup / name).read_text(), "original " + name)
            self.assertIn("agent_maintenance", (scripts / name).read_text())
        self.assertTrue((scripts / "agent-maintenance/host.py").is_file())


if __name__ == "__main__":
    unittest.main()
