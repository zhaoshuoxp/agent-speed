import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
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
                                                        "CLAUDE_ARCHIVE_DIR": str(self.archived)})
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

    def test_claude_backup_archive_restore_and_delete_require_verified_content(self):
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
        request["operation"] = "delete"
        plan = host.make_plan(request)
        self.assertTrue(host.apply_plan({"plan": plan, "confirm": plan["plan_id"], "verified_backup_sha256": proof["sha256"]})["all_verified"])
        self.assertFalse(self.claude_path.exists())
        self.assertFalse(self.tool_output.exists())
        self.assertTrue(Path(proof["path"]).exists())

    def test_stale_backup_and_unbacked_children_are_rejected(self):
        proof = self.make_backup({"sessions": [PARENT], "tool": "codex"})
        request = {"operation": "delete", "selector": {"sessions": [PARENT], "tool": "codex"},
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
        proof = self.make_backup({"sessions": [CLAUDE], "tool": "claude"})
        plan = host.make_plan({"operation": "delete", "selector": {"sessions": [CLAUDE]},
                               "backup_proof": proof["manifest"], "backup_sha256": proof["sha256"]})
        with self.assertRaises(ValueError):
            host.apply_plan({"plan": plan, "confirm": "wrong", "verified_backup_sha256": proof["sha256"]})
        self.claude_path.write_text("changed")
        with self.assertRaises(RuntimeError):
            host.apply_plan({"plan": plan, "confirm": plan["plan_id"], "verified_backup_sha256": proof["sha256"]})
        self.assertTrue(self.claude_path.exists())

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
                                  "--backup", str(destination), "--output", str(plan_path), "--json"])
        self.assertEqual(status, 0)
        plan = json.loads(plan_path.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            status = client.main(["apply", str(plan_path), "--confirm", plan["plan"]["plan_id"], "--json"])
        self.assertEqual(status, 0)
        self.assertFalse(self.claude_path.exists())
        self.assertTrue(destination.exists())


if __name__ == "__main__":
    unittest.main()
