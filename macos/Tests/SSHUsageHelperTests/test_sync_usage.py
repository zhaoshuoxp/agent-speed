import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

HELPER = pathlib.Path(__file__).resolve().parents[2] / "Resources/SSHUsage/sync_usage.py"
SPEC = importlib.util.spec_from_file_location("ssh_usage", HELPER)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name).resolve()
        self.root = self.base / "AgentSpeed/SSHUsage"
        self.root.mkdir(parents=True)
        self.data = self.root / "data"
        self.store = {"source": "codex", "kind": "sessions", "path": "/remote/.codex/sessions",
                      "destination": "codex/thor/sessions"}
        self.config = {"state": str(self.root), "data": str(self.data), "local": [],
                       "hosts": [{"host": "thor", "stores": [self.store]}]}
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(self.config))
        self.original = self.base / "server-original.jsonl"
        self.original.write_text('{"usage":123}\n')
        old_ns = int((time.time() - 45 * 86400) * 1_000_000_000)
        os.utime(self.original, ns=(old_ns, old_ns))
        self.cached = self.data / "codex/thor/sessions/log.jsonl"
        self.cached.parent.mkdir(parents=True)
        shutil.copy2(self.original, self.cached)
        self.local = self.data / "codex/local/sessions/local.jsonl"
        self.local.parent.mkdir(parents=True)
        self.local.write_text("local original")
        info = self.cached.stat()
        self.version = {"size": info.st_size, "modified_ns": info.st_mtime_ns}
        self.key = "codex/thor/sessions/log.jsonl"
        sync.atomic_json(self.root / "remote-index.json", {self.key: self.version})
        self.history = self.root.parent / "history.sqlite"
        with sqlite3.connect(self.history) as db:
            db.execute("create table files(path text primary key,size integer,modified integer)")
            db.execute("insert into files values(?,?,?)", (str(self.cached), info.st_size, info.st_mtime_ns))
            db.execute("create table contributions(day text,input integer,output integer)")
            db.execute("insert into contributions values('2026-09-01',1000,123)")
        db.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_cleanup_keeps_history_local_logs_and_server_original(self):
        before = self.history.read_bytes()
        result = sync.clear_cache(self.path, days=30)
        self.assertEqual(result["removed_files"], 1)
        self.assertFalse(self.cached.exists())
        self.assertEqual(self.original.read_text(), '{"usage":123}\n')
        self.assertEqual(self.local.read_text(), "local original")
        self.assertEqual(self.history.read_bytes(), before)
        excluded = sync.read_json(self.root / "cleaned-files.json", {})
        rows = [["log.jsonl", self.version["size"], self.version["modified_ns"]]]
        selected, _, skipped = sync.select_files(self.store, rows, excluded)
        self.assertEqual(selected, [])
        self.assertEqual(skipped, 1)
        rows[0][1] += 1
        selected, _, _ = sync.select_files(self.store, rows, excluded)
        self.assertEqual(selected, ["log.jsonl"])

    def test_pending_history_is_not_deleted(self):
        self.cached.write_text('{"usage":4567}\n')
        result = sync.clear_cache(self.path, days=0)
        self.assertEqual(result["removed_files"], 0)
        self.assertEqual(result["pending_files"], 1)
        self.assertTrue(self.cached.exists())

    def test_preview_changes_nothing(self):
        result = sync.clear_cache(self.path, days=0, dry_run=True)
        self.assertEqual(result["removed_files"], 1)
        self.assertTrue(self.cached.exists())
        self.assertFalse((self.root / "cleaned-files.json").exists())

    def test_recent_files_and_symlinks_are_kept(self):
        os.utime(self.cached, None)
        (self.cached.parent / "outside.jsonl").symlink_to(self.original)
        result = sync.clear_cache(self.path, days=7)
        self.assertEqual(result["removed_files"], 0)
        self.assertTrue(self.cached.exists())
        self.assertTrue(self.original.exists())

    def test_cache_sizes_exclude_local_logs_and_links(self):
        (self.cached.parent / "outside.jsonl").symlink_to(self.original)
        with mock.patch.object(sync, "service_loaded", return_value=True):
            result = sync.cache_info(self.path)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["bytes"], self.cached.stat().st_size)
        self.assertEqual(result["hosts"][0]["codex_files"], 1)

    def test_traversal_and_host_symlinks_are_rejected(self):
        self.config["hosts"][0]["stores"][0]["destination"] = "codex/thor/../../../original"
        with self.assertRaises(ValueError):
            sync.validate_config(self.config, self.root)
        self.config["hosts"][0]["stores"][0]["destination"] = "codex/thor/sessions"
        linked = self.data / "codex/thor/link"
        linked.symlink_to(self.base, target_is_directory=True)
        self.config["hosts"][0]["stores"][0]["destination"] = "codex/thor/link"
        with self.assertRaises(ValueError):
            sync.validate_config(self.config, self.root)

    def test_cleanup_cannot_race_with_sync(self):
        with sync.state_lock(self.root):
            with self.assertRaises(RuntimeError):
                sync.clear_cache(self.path, days=0)
        self.assertTrue(self.cached.exists())

    def test_cleanup_requires_history_and_remote_version(self):
        (self.root / "remote-index.json").unlink()
        self.assertEqual(sync.clear_cache(self.path, days=0)["pending_files"], 1)
        self.history.unlink()
        with self.assertRaises(RuntimeError):
            sync.clear_cache(self.path, days=0)
        self.assertTrue(self.cached.exists())

    def test_deleted_local_original_releases_its_indexed_mirror(self):
        source = self.base / "local-originals"
        source.mkdir()
        original = source / "local.jsonl"
        original.write_text("saved usage")
        self.local.unlink()
        os.link(original, self.local)
        info = self.local.stat()
        with sqlite3.connect(self.history) as db:
            db.execute("insert into files values(?,?,?)", (str(self.local), info.st_size, info.st_mtime_ns))
        db.close()
        self.config["local"] = [{"path": str(source), "source": "codex", "destination": "codex/local/sessions"}]
        original.unlink()
        result = sync.sync_local(self.config)
        self.assertEqual(result["stale_links_removed"], 1)
        self.assertFalse(self.local.exists())
        with sqlite3.connect(self.history) as db:
            self.assertEqual(db.execute("select output from contributions").fetchone()[0], 123)
        db.close()


if __name__ == "__main__":
    unittest.main()
