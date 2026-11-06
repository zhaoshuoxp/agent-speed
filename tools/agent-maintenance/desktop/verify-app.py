#!/usr/bin/env python3
"""Check the packaged app against a guardian-only store using the selected Python."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("app", type=Path)
parser.add_argument("--python", default=sys.executable)
args = parser.parse_args()
app = args.app.resolve()
info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
assert info["CFBundleIconFile"] == "AgentMaintenance"
assert (app / "Contents/Resources/AgentMaintenance.icns").is_file()
subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
with tempfile.TemporaryDirectory(prefix="maintenance-app-check-") as temporary:
    root = Path(temporary)
    codex = root / ".codex"
    claude = root / ".claude"
    codex.mkdir()
    old = int(time.time() - 45 * 86400)
    with contextlib.closing(sqlite3.connect(codex / "state_5.sqlite")) as database:
        database.execute("create table projects(id text,title text)")
        database.execute("create table threads(id text primary key,rollout_path text,created_at integer,updated_at integer,cwd text,title text,source text,archived integer,thread_source text)")
        for number in range(1, 41):
            identifier = "00000000-0000-4000-8000-" + format(number, "012x")
            parent = "00000000-0000-4000-8000-" + format(number - 27, "012x") if number > 27 else None
            path = codex / "sessions" / ("rollout-" + identifier + ".jsonl")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": identifier, "parent_thread_id": parent, "cwd": str(root)}}) + "\n")
            os.utime(path, (old, old))
            database.execute("insert into threads values(?,?,?,?,?,?,?,?,?)", (identifier, str(path), old, old,
                             str(root), "Guardian fixture", '{"subagent":{"other":"guardian"}}', 0, "guardian_review"))
        database.commit()
    conversation = claude / "projects/example/00000000-0000-4000-8000-000000000099.jsonl"
    conversation.parent.mkdir(parents=True)
    conversation.write_text(json.dumps({"type": "user", "cwd": str(root), "message": {"content": "Fixture"}}) + "\n")
    configuration = root / "config.json"
    configuration.write_text(json.dumps({"hosts": ["thor", "loki", "freya", "bordeaux", "champagne"]}))
    stream_script = root / "stream-fixture.py"
    acknowledgement = Path(str(stream_script) + ".ack")
    stream_script.write_text("import json, sys, time\nfrom pathlib import Path\n"
                             "print('Stream fixture started', file=sys.stderr, flush=True)\n"
                             "deadline=time.monotonic()+5\n"
                             "while not Path(" + repr(str(acknowledgement)) + ").exists():\n"
                             "    if time.monotonic()>deadline: raise RuntimeError('App buffered stderr')\n"
                             "    time.sleep(0.01)\n"
                             "print(json.dumps({'stream_completed':True,'payload':'x'*131072}), flush=True)\n"
                             "print('Fixture finished\\n'*8192, file=sys.stderr, flush=True)\n")
    environment = dict(os.environ, AGENT_MAINTENANCE_CONFIG=str(configuration), CODEX_HOME=str(codex),
                       CLAUDE_CONFIG_DIR=str(claude), CLAUDE_ARCHIVE_DIR=str(root / "claude-archive"),
                       AGENT_MAINTENANCE_PYTHON=args.python, AGENT_MAINTENANCE_SELF_CHECK_SCRIPT=str(stream_script))
    environment.pop("AGENT_MAINTENANCE_HOSTS", None)
    response = subprocess.run([str(app / "Contents/MacOS/AgentMaintenance"), "--self-check"], env=environment,
                              capture_output=True, text=True, timeout=45)
    if response.returncode:
        raise RuntimeError(response.stderr[-4000:])
    report = json.loads(response.stdout)
    assert report["hosts"] == ["local", "thor", "loki", "freya", "bordeaux", "champagne"], report
    targets = ["thor", "loki", "freya", "bordeaux", "champagne"]
    assert report["upgrade_hosts"] == targets, report
    assert report["upgrade_arguments"] == ["upgrade", "--tool", "all"] + [argument for name in targets for argument in ("--host", name)], report
    assert report["execution_host"], report
    assert report["codex"]["total_records"] == 40, report
    assert report["codex"]["visible_records"] == 27, report
    assert report["codex"]["subagent_records"] == 40, report
    assert report["claude"]["total_records"] == 1, report
    assert report["iconBundled"], report
    assert report["operation_records"] == "save and clear passed", report
    assert report["live_output"] == "delivered before process exit", report
    print(json.dumps({"app_version": info["CFBundleShortVersionString"], "python": report["python"],
                      "hosts": len(report["hosts"]), "upgrade_targets": len(targets), "codex_records": 40, "default_visible_codex": 27,
                      "claude_records": 1, "icon_bundled": True, "signature": "passed", "self_check": "passed",
                      "live_output": report["live_output"], "operation_records": report["operation_records"]}, indent=2))
