#!/usr/bin/env python3
"""Exercise native archive/delete against an isolated Codex home, without model calls."""
import contextlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import host
import agent_maintenance as client


def main():
    executable = host.find_cli("codex")
    if not executable:
        raise RuntimeError("Codex CLI is required for native validation")
    with tempfile.TemporaryDirectory(prefix="native-codex-", dir=Path(__file__).parent) as directory:
        root = Path(directory).resolve()
        codex_home = root / ".codex"
        codex_home.mkdir()
        project = root / "ExampleProject"
        project.mkdir()
        environment = dict(os.environ, CODEX_HOME=str(codex_home), CLAUDE_CONFIG_DIR=str(root / ".claude"),
                           CLAUDE_ARCHIVE_DIR=str(root / ".claude-archive"))
        error_log = root / "app-server.log"
        messages = queue.Queue()
        with error_log.open("wb") as errors:
            process = subprocess.Popen([str(executable), "app-server"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1, env=environment)
            def reader():
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            threading.Thread(target=reader, daemon=True).start()
            def rpc(identifier, method, parameters):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                                "method": method, "params": parameters}) + "\n")
                process.stdin.flush()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    response = messages.get(timeout=max(0.1, deadline - time.monotonic()))
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        return response["result"]
                raise RuntimeError("App-server did not respond")
            try:
                rpc(1, "initialize", {"clientInfo": {"name": "agent_maintenance_validation", "version": host.VERSION},
                                       "capabilities": {"experimentalApi": True}})
                process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
                process.stdin.flush()
                started = rpc(2, "thread/start", {"cwd": str(project), "persistExtendedHistory": True})
                sid = started["thread"]["id"]
                rpc(3, "thread/inject_items", {"threadId": sid, "items": [
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "Maintenance validation fixture; no model turn is requested."}]}]})
            finally:
                process.stdin.close()
                process.wait(timeout=20)
                process.stdout.close()
        # Fixture records are made old only inside the temporary home.
        database = codex_home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as db:
            row = db.execute("select rollout_path from threads where id=?", (sid,)).fetchone()
            if not row:
                raise RuntimeError("The native CLI did not persist the fixture thread")
            old = int(time.time() - 45 * 86400)
            db.execute("update threads set updated_at=? where id=?", (old, sid))
            db.commit()
        os.utime(row[0], (old, old))
        backup = root / "native-session.zip"
        saved_environment = {key: os.environ.get(key) for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_ARCHIVE_DIR")}
        os.environ.update({key: environment[key] for key in saved_environment})
        try:
            with backup.open("wb") as output:
                host.backup({"sessions": [sid], "tool": "codex"}, output)
            proof = client.verify_backup(backup)
            outcomes = []
            for operation in ("archive", "unarchive", "delete"):
                request = {"operation": operation, "selector": {"sessions": [sid], "tool": "codex"}}
                if operation == "archive":
                    request.update(backup_proof=proof["manifest"], backup_sha256=proof["sha256"], backup_path=proof["path"])
                # Native archive state updates are also aged within the fixture before the next action.
                if operation != "archive":
                    with contextlib.closing(sqlite3.connect(database)) as db:
                        db.execute("update threads set updated_at=? where id=?", (old, sid))
                        current_path = db.execute("select rollout_path from threads where id=?", (sid,)).fetchone()[0]
                        db.commit()
                    os.utime(current_path, (old, old))
                plan = host.make_plan(request)
                apply_request = {"plan": plan, "confirm": plan["plan_id"]}
                if operation == "archive":
                    apply_request["verified_backup_sha256"] = proof["sha256"]
                result = host.apply_plan(apply_request)
                outcomes.append({"operation": operation, "verified": result["all_verified"], "results": result["results"]})
                print(json.dumps(outcomes[-1]), flush=True)
                if not result["all_verified"]:
                    raise RuntimeError(json.dumps(result))
            report = {"native_codex_validation": "passed", "isolated_home": True, "model_calls": 0,
                      "operations": outcomes, "backup_verified": True}
            (Path(__file__).parent / "native-codex-validation.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
        finally:
            for key, value in saved_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    main()
