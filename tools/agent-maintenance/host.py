#!/usr/bin/env python3
"""Host-side maintenance engine. Read-only actions are the default."""
import base64
import contextlib
import datetime
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from urllib.request import Request, urlopen
from urllib.error import URLError

VERSION = "0.1.1"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
RECENT_SECONDS = 300


def roots():
    home = Path.home()
    config = Path(os.environ.get("AGENT_MAINTENANCE_CONFIG", str(home / ".config/agent-maintenance/config.json"))).expanduser()
    settings = json.loads(config.read_text()) if config.is_file() else {}
    configured = settings.get("data_roots", {})
    return {"codex": Path(os.environ.get("CODEX_HOME") or configured.get("codex") or home / ".codex").expanduser(),
            "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or configured.get("claude") or home / ".claude").expanduser(),
            "claude-archive": Path(os.environ.get("CLAUDE_ARCHIVE_DIR", str(home / ".claude-archive"))).expanduser()}


def connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma query_only=1")
    return connection


def columns(connection, table):
    return {row[1] for row in connection.execute('pragma table_info("' + table.replace('"', '""') + '")')}


def regular_files(folder):
    folder = Path(folder)
    if not folder.exists() or folder.is_symlink():
        return
    if folder.is_file():
        yield folder
        return
    for directory, names, files in os.walk(folder, followlinks=False):
        names[:] = [name for name in names if not os.path.islink(os.path.join(directory, name))]
        for name in files:
            path = Path(directory) / name
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    yield path
            except FileNotFoundError:
                continue


def identity(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("A selected path is not a regular file: " + str(path))
    return {"size": info.st_size, "modified_ns": info.st_mtime_ns, "device": info.st_dev,
            "inode": info.st_ino, "links": info.st_nlink, "disk_bytes": info.st_blocks * 512}


def fingerprint(path):
    value = identity(path)
    return {key: value[key] for key in ("size", "modified_ns", "device", "inode")}


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp(value):
    try:
        value = float(value or 0)
        return value / 1000 if value > 10_000_000_000 else value
    except (TypeError, ValueError):
        return 0


def state_databases(root):
    values = list(root.glob("state_*.sqlite")) + list((root / "sqlite").glob("state_*.sqlite"))
    def key(path):
        match = re.search(r"state_(\d+)", path.name)
        wal = Path(str(path) + "-wal")
        modified = max(path.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0)
        return (int(match.group(1)) if match else -1, modified)
    seen, result = set(), []
    for path in sorted(values, key=key, reverse=True):
        if path.resolve() not in seen:
            seen.add(path.resolve())
            result.append(path)
    return result


def scan_codex():
    root = roots()["codex"]
    records, edges, errors, busy = {}, {}, [], set()
    for database in state_databases(root):
        try:
            with contextlib.closing(connect(database)) as db:
                fields = columns(db, "threads")
                if not {"id", "rollout_path"}.issubset(fields):
                    continue
                projects = {}
                if {"id", "name"}.issubset(columns(db, "projects")):
                    projects = {row["id"]: row["name"] for row in db.execute("select id,name from projects")}
                if columns(db, "thread_spawn_edges"):
                    for parent, child in db.execute("select parent_thread_id,child_thread_id from thread_spawn_edges"):
                        edges[child] = parent
                for raw in db.execute("select * from threads"):
                    row = dict(raw)
                    sid = row["id"]
                    if not UUID.fullmatch(sid):
                        continue
                    if sid in records:
                        continue
                    path = Path(row["rollout_path"]).expanduser()
                    try:
                        info = identity(path)
                    except (OSError, ValueError):
                        info = {"size": 0, "disk_bytes": 0, "links": 0, "modified_ns": 0}
                    cwd = row.get("cwd") or ""
                    project_id = row.get("project_id")
                    source = row.get("source") or ""
                    parent = row.get("attached_thread_parent_id") or edges.get(sid)
                    if not parent:
                        try:
                            item = json.loads(source)
                            parent = item.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
                        except (ValueError, AttributeError):
                            pass
                    is_subagent = "subagent" in source or row.get("thread_source") in ("subagent", "guardian_review")
                    if is_subagent and not parent and path.is_file() and path.suffix == ".jsonl":
                        with path.open("rb") as handle:
                            prefix = handle.read(16384).split(b'"base_instructions"', 1)[0]
                        match = re.search(rb'"parent_thread_id"\s*:\s*"([0-9a-fA-F-]{36})"', prefix)
                        if match:
                            parent = match.group(1).decode()
                    records[sid] = {"tool": "codex", "id": sid,
                                    "title": (row.get("name") or row.get("title") or sid)[:300],
                                    "cwd": cwd, "project_id": project_id,
                                    "project": projects.get(project_id) or Path(cwd).name or "Unassigned",
                                    "archived": bool(row.get("archived")), "parent_id": parent,
                                    "is_subagent": is_subagent or bool(parent),
                                    "updated_at": timestamp(row.get("updated_at")),
                                    "path": str(path), "base": str(root), "database": str(database),
                                    "bytes": info["size"], "disk_bytes": info["disk_bytes"],
                                    "links": info["links"], "source": source,
                                    "file_modified_at": info["modified_ns"] / 1e9}
        except (OSError, sqlite3.Error) as error:
            errors.append({"path": str(database), "error": str(error)})
    for directory, archived in ((root / "sessions", False), (root / "archived_sessions", True)):
        for path in regular_files(directory):
            if not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
                continue
            match = UUID.search(path.name)
            if not match or match.group(0) in records:
                continue
            sid, metadata = match.group(0), {}
            if path.suffix == ".jsonl":
                try:
                    with path.open("rb") as handle:
                        first = handle.readline(2 << 20)
                        if first.endswith(b"\n"):
                            metadata = json.loads(first).get("payload", {})
                except (OSError, ValueError, AttributeError):
                    pass
            cwd = metadata.get("cwd") or ""
            info = identity(path)
            records[sid] = {"tool": "codex", "id": sid, "title": metadata.get("name") or sid,
                            "cwd": cwd, "project_id": None, "project": Path(cwd).name or "Unassigned",
                            "archived": archived, "parent_id": edges.get(sid),
                            "is_subagent": bool(edges.get(sid)) or metadata.get("thread_source") in ("subagent", "guardian_review"),
                            "updated_at": info["modified_ns"] / 1e9, "path": str(path),
                            "base": str(root), "database": None, "bytes": info["size"],
                            "disk_bytes": info["disk_bytes"], "links": info["links"], "source": "unindexed"}
    for history in list(root.glob("thread_history_*.sqlite")) + list((root / "sqlite").glob("thread_history_*.sqlite")):
        try:
            with contextlib.closing(connect(history)) as db:
                fields = columns(db, "thread_turns")
                if {"thread_id", "status", "completed_at"}.issubset(fields):
                    for sid, status in db.execute("select thread_id,status from thread_turns where completed_at is null"):
                        if status.lower() not in ("completed", "failed", "interrupted", "cancelled", "aborted"):
                            busy.add(sid)
        except sqlite3.Error:
            continue
    for sid, row in records.items():
        row["parent_id"] = row["parent_id"] or edges.get(sid)
        row["busy"] = sid in busy or time.time() - max(row["updated_at"], row.get("file_modified_at", 0)) < RECENT_SECONDS
    return list(records.values()), errors


def sample_jsonl(path):
    with path.open("rb") as handle:
        head = handle.read(256 << 10)
        size = handle.seek(0, os.SEEK_END)
        offset = max(0, size - (64 << 10))
        handle.seek(offset)
        tail = handle.read()
    first = head.splitlines() if head.endswith(b"\n") else head.splitlines()[:-1]
    blobs = first + (tail.splitlines()[1:] if offset else tail.splitlines())
    for line in blobs:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                yield value
        except (ValueError, UnicodeError):
            continue


def text_blocks(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(item.get("text", "") for item in value if isinstance(item, dict)
                         and item.get("type") in ("text", "input_text", "output_text"))
    return ""


def scan_claude():
    rows, errors = [], []
    for kind, archived in (("claude", False), ("claude-archive", True)):
        root = roots()[kind]
        for path in (root / "projects").glob("*/*.jsonl"):
            if not UUID.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                values = list(sample_jsonl(path))
                first = next((text_blocks((value.get("message") or {}).get("content"))
                              for value in values if value.get("type") == "user" and not value.get("isMeta")), "")
                cwd = next((value["cwd"] for value in values if isinstance(value.get("cwd"), str)), "")
                custom = next((value.get("customTitle") or value.get("title") for value in reversed(values)
                               if value.get("type") == "custom-title"), None)
                summary = next((value.get("summary") for value in reversed(values)
                                if value.get("type") == "summary"), None)
                info = identity(path)
                rows.append({"tool": "claude", "id": path.stem,
                             "title": (custom or summary or first or path.stem)[:300],
                             "cwd": cwd, "project_id": None,
                             "project": Path(cwd).name or path.parent.name, "archived": archived,
                             "parent_id": None, "is_subagent": False, "updated_at": info["modified_ns"] / 1e9,
                             "path": str(path), "base": str(root), "database": None,
                             "bytes": info["size"], "disk_bytes": info["disk_bytes"], "links": info["links"],
                             "source": "transcript", "busy": time.time() - info["modified_ns"] / 1e9 < RECENT_SECONDS})
            except (OSError, ValueError) as error:
                errors.append({"path": str(path), "error": str(error)})
    return rows, errors


def storage_info():
    result = []
    for tool, root in roots().items():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_symlink():
                continue
            logical = disk = count = 0
            seen = set()
            for path in regular_files(child):
                try:
                    info = path.stat()
                except OSError:
                    continue
                logical += info.st_size
                count += 1
                identity_key = (info.st_dev, info.st_ino)
                if identity_key not in seen:
                    disk += info.st_blocks * 512
                    seen.add(identity_key)
            if count:
                result.append({"tool": tool, "name": child.name, "path": str(child),
                               "bytes": logical, "disk_bytes": disk, "files": count})
    return result


def machine_id():
    return hashlib.sha256((os.uname().nodename + "\0" + str(Path.home().resolve())).encode()).hexdigest()[:24]


def inventory(include_storage=False):
    codex, errors = scan_codex()
    claude, more = scan_claude()
    records = codex + claude
    index = {(row["tool"], row["id"]): row for row in records}
    for row in records:
        owner, seen = row, set()
        while owner["parent_id"] and (owner["tool"], owner["parent_id"]) in index and owner["id"] not in seen:
            seen.add(owner["id"])
            owner = index[(owner["tool"], owner["parent_id"])]
        row["project"] = owner["project"]
        row["project_id"] = owner.get("project_id")
        row["project_path"] = owner.get("project_path", owner["cwd"])
    children = {}
    for row in records:
        if row["parent_id"]:
            children.setdefault((row["tool"], row["parent_id"]), []).append(row["id"])
    for row in records:
        row["children"] = children.get((row["tool"], row["id"]), [])
    result = {"version": VERSION, "machine_id": machine_id(), "hostname": os.uname().nodename,
            "home": str(Path.home()), "sessions": records, "errors": errors + more,
            "storage": storage_info() if include_storage else []}
    result["diagnostics"] = diagnostics(result)
    return result


def diagnostics(data):
    locations = roots()
    result = {"home": str(Path.home()), "errors": data["errors"]}
    for tool in ("codex", "claude"):
        rows = [row for row in data["sessions"] if row["tool"] == tool]
        visible = selected_sessions(data, {"tool": tool}, expand=False)
        result[tool] = {"root": str(locations[tool]), "exists": locations[tool].is_dir(),
                        "total_records": len(rows), "visible_records": len(visible),
                        "subagent_records": sum(bool(row.get("is_subagent")) for row in rows),
                        "missing_transcripts": sum(not Path(row["path"]).is_file() for row in rows)}
        if tool == "codex":
            result[tool]["databases"] = [str(path) for path in state_databases(locations[tool])]
    return result


def selected_sessions(data, selector, expand=True):
    tool = selector.get("tool", "all")
    wanted = set(selector.get("sessions") or [])
    project = selector.get("project")
    query = selector.get("query", "").lower()
    archived = selector.get("archived", "all")
    known = {(row["tool"], row["id"]) for row in data["sessions"]}
    rows = []
    for row in data["sessions"]:
        if tool != "all" and row["tool"] != tool:
            continue
        if wanted and row["id"] not in wanted:
            continue
        if project and project not in (row["project"], row.get("project_id"), row["cwd"], row.get("project_path")):
            if not (project.startswith("/") and row["cwd"].startswith(project.rstrip("/") + "/")):
                continue
        if query and query not in (row["title"] + " " + row["cwd"] + " " + row["id"]).lower():
            continue
        if archived != "all" and row["archived"] != (archived == "yes"):
            continue
        if (not wanted and row.get("is_subagent", bool(row["parent_id"]))
                and (row["tool"], row["parent_id"]) in known and not selector.get("include_subagents")):
            continue
        rows.append(row)
    if wanted - {row["id"] for row in rows}:
        raise ValueError("One or more selected session IDs were not found")
    if expand:
        included = {(row["tool"], row["id"]) for row in rows}
        while True:
            additions = [row for row in data["sessions"] if row["parent_id"]
                         and (row["tool"], row["parent_id"]) in included
                         and (row["tool"], row["id"]) not in included]
            if not additions:
                break
            rows += additions
            included.update((row["tool"], row["id"]) for row in additions)
    return rows


def session_files(row):
    root, sid = Path(row["base"]), row["id"]
    candidates = [Path(row["path"])]
    if row["tool"] == "codex":
        candidates += [root / "generated_images" / sid, root / "attachments" / sid]
    else:
        candidates += [Path(row["path"]).with_suffix(""), root / "file-history" / sid,
                       root / "session-env" / sid]
        candidates += list((root / "todos").glob(sid + "*.json"))
    seen = set()
    for candidate in candidates:
        for path in regular_files(candidate):
            resolved = path.resolve()
            if root.resolve() not in resolved.parents:
                raise ValueError("A session payload is outside its configured agent home")
            if resolved not in seen:
                seen.add(resolved)
                yield path


def encode_value(value):
    if isinstance(value, bytes):
        return {"__base64__": base64.b64encode(value).decode()}
    return value


def zip_member(archive, name, blocks, manifest, kind, **metadata):
    digest, count = hashlib.sha256(), 0
    entry = zipfile.ZipInfo(name)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100600 << 16
    with archive.open(entry, "w", force_zip64=True) as destination:
        for block in blocks:
            if isinstance(block, str):
                block = block.encode("utf-8")
            destination.write(block)
            digest.update(block)
            count += len(block)
    manifest["files"].append({"name": name, "kind": kind, "size": count, "sha256": digest.hexdigest(), **metadata})


def file_blocks(path, expected, references):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        actual = {"size": info.st_size, "modified_ns": info.st_mtime_ns,
                  "device": info.st_dev, "inode": info.st_ino}
        if expected != actual:
            raise RuntimeError("A selected file changed before backup: " + str(path))
        remaining, carry = info.st_size, b""
        while remaining:
            block = handle.read(min(1 << 20, remaining))
            if not block:
                raise RuntimeError("A selected file was truncated during backup")
            probe = carry + block
            for match in re.finditer(rb"/(attachments|generated_images)/([0-9a-fA-F-]{36})", probe):
                references.add((match.group(1).decode(), match.group(2).decode()))
            carry = probe[-512:]
            remaining -= len(block)
            yield block
        info = os.fstat(handle.fileno())
        if (info.st_size, info.st_mtime_ns) != (expected["size"], expected["modified_ns"]):
            raise RuntimeError("A selected file changed during backup; retry when the conversation is idle")


def readable_blocks(row):
    title = html.escape(row["title"])
    yield '<!doctype html><meta charset="utf-8"><title>' + title + '</title>'
    yield '<style>body{font:18px/1.6 system-ui;max-width:1000px;margin:40px auto;padding:0 24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #bbb;margin-top:24px}</style>'
    yield '<h1>' + title + '</h1><p>Session: ' + row["id"] + '</p><p>Full tool records and embedded images are preserved in the original transcript. Referenced media is included in the backup.</p>'
    path = Path(row["path"])
    if path.suffix != ".jsonl":
        yield '<p>This compressed transcript is preserved as an original file.</p>'
        return
    seen = set()
    with path.open("rb") as handle:
        while True:
            line = handle.readline((32 << 20) + 1)
            if not line:
                break
            if len(line) > (32 << 20):
                while line and not line.endswith(b"\n"):
                    line = handle.readline(1 << 20)
                yield '<p>A large record is preserved in the original transcript.</p>'
                continue
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            role, body = None, ""
            if row["tool"] == "claude" and value.get("type") in ("user", "assistant"):
                role, body = value["type"], text_blocks((value.get("message") or {}).get("content"))
            elif row["tool"] == "codex":
                payload = value.get("payload") or {}
                if value.get("type") == "response_item" and payload.get("type") == "message":
                    role, body = payload.get("role"), text_blocks(payload.get("content"))
                elif value.get("type") == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
                    role = "user" if payload["type"] == "user_message" else "assistant"
                    body = payload.get("message") or ""
            if role not in ("user", "assistant") or not body:
                continue
            key = (role, value.get("timestamp"), hashlib.sha256(body.encode()).digest())
            if key in seen:
                continue
            seen.add(key)
            yield '<section><h2>' + role.capitalize() + '</h2><pre>' + html.escape(body) + '</pre></section>'


def database_members(archive, rows, manifest):
    ids = [row["id"] for row in rows if row["tool"] == "codex"]
    if not ids:
        return
    root = roots()["codex"]
    databases = state_databases(root) + list(root.glob("thread_history_*.sqlite")) + list((root / "sqlite").glob("thread_history_*.sqlite"))
    markers = ",".join("?" for _ in ids)
    for index, database in enumerate(dict.fromkeys(databases)):
        with contextlib.closing(connect(database)) as db:
            schemas = {}
            for table, sql in db.execute("select name,sql from sqlite_master where type='table'"):
                if table.startswith("_") or not re.fullmatch(r"[A-Za-z0-9_]+", table):
                    continue
                fields = columns(db, table)
                if table == "threads":
                    where, parameters = '"id" in (' + markers + ')', ids
                elif "thread_id" in fields:
                    where, parameters = '"thread_id" in (' + markers + ')', ids
                elif {"parent_thread_id", "child_thread_id"}.issubset(fields):
                    where = '"parent_thread_id" in (' + markers + ') or "child_thread_id" in (' + markers + ')'
                    parameters = ids + ids
                else:
                    continue
                schemas[table] = sql
                def blocks(table=table, where=where, parameters=parameters):
                    for item in db.execute('select * from "' + table + '" where ' + where, parameters):
                        yield json.dumps({key: encode_value(item[key]) for key in item.keys()}, ensure_ascii=True) + "\n"
                zip_member(archive, f"metadata/database-{index}/{table}.jsonl", blocks(), manifest, "database-rows",
                           source_database=str(database), table=table)
            zip_member(archive, f"metadata/database-{index}/schema.json",
                       [json.dumps(schemas, ensure_ascii=True)], manifest, "database-schema")


def backup(selector, output):
    if not (selector.get("sessions") or selector.get("project") or selector.get("all")):
        raise ValueError("Choose a project, session IDs, or an explicit all-sessions selection")
    data = inventory()
    rows = selected_sessions(data, selector)
    if not rows:
        raise ValueError("No conversations match the selection")
    active = [row["id"] for row in rows if row["busy"]]
    if active:
        raise RuntimeError("Selected conversations are active or were updated within five minutes: " + ", ".join(active))
    manifest = {"format": "agent-maintenance-backup", "format_version": 1, "created_at": time.time(),
                "machine_id": data["machine_id"], "hostname": data["hostname"], "sessions": rows,
                "files": [], "restore_mode": "standalone-files", "global_credential_files_included": False}
    written, references = set(), set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for row in rows:
            for path in session_files(row):
                name = "files/" + ("codex" if row["tool"] == "codex" else "claude-archive" if row["archived"] else "claude") + "/" + path.relative_to(row["base"]).as_posix()
                if name in written:
                    continue
                written.add(name)
                before = fingerprint(path)
                zip_member(archive, name, file_blocks(path, before, references), manifest,
                           "transcript" if str(path) == row["path"] else "session-file",
                           owner_id=row["id"], tool=row["tool"], source_path=str(path), identity=before)
            zip_member(archive, "readable/" + row["tool"] + "-" + row["id"] + ".html",
                       readable_blocks(row), manifest, "readable-export", owner_id=row["id"], tool=row["tool"])
        root = roots()["codex"]
        for kind, identifier in sorted(references):
            if not UUID.fullmatch(identifier):
                continue
            for path in regular_files(root / kind / identifier):
                name = "files/codex/" + path.relative_to(root).as_posix()
                if name in written:
                    continue
                written.add(name)
                before = fingerprint(path)
                zip_member(archive, name, file_blocks(path, before, set()), manifest, "referenced-media",
                           source_path=str(path), identity=before, tool="codex")
        database_members(archive, rows, manifest)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2))


def cleanup_files(categories, days, selected_tool="all"):
    if days < 1:
        raise ValueError("Cleanup requires a minimum age of one day")
    if not set(categories).issubset({"logs", "temp"}):
        raise ValueError("Cleanup categories are logs and temp")
    places = []
    for tool, root in roots().items():
        if tool == "claude-archive" or selected_tool not in ("all", tool):
            continue
        if "logs" in categories:
            places += [(tool, "logs", root / name) for name in ("log", "logs", "debug")]
        if "temp" in categories:
            places += [(tool, "temp", root / name) for name in ("tmp", ".tmp", "shell_snapshots", "shell-snapshots", "cache")]
    if "logs" in categories and sys.platform == "darwin" and selected_tool in ("all", "codex"):
        places += [("codex", "logs", Path.home() / "Library/Logs/com.openai.codex")]
    cutoff = time.time() - days * 86400
    candidates = []
    for tool, category, folder in places:
        for path in regular_files(folder):
            info = identity(path)
            if info["modified_ns"] / 1e9 >= cutoff or path.suffix in (".sqlite", ".db"):
                continue
            candidates.append({"path": str(path), "tool": tool, "category": category, **info})
    return candidates


def plan_digest(plan):
    return hashlib.sha256(json.dumps({key: value for key, value in plan.items() if key != "plan_id"},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def make_plan(request):
    operation = request["operation"]
    if operation not in ("archive", "unarchive", "delete", "cleanup"):
        raise ValueError("Unsupported maintenance operation")
    result = {"format": "agent-maintenance-plan", "format_version": 1, "created_at": time.time(),
              "machine_id": machine_id(), "operation": operation, "items": [], "sessions": [],
              "backup_sha256": request.get("backup_sha256"), "backup_path": request.get("backup_path")}
    if operation == "cleanup":
        result["categories"] = request.get("categories") or ["logs", "temp"]
        result["days"] = int(request.get("days", 30))
        result["tool"] = request.get("selector", {}).get("tool", "all")
        result["items"] = cleanup_files(result["categories"], result["days"], result["tool"])
    else:
        selector = request["selector"]
        if not (selector.get("sessions") or selector.get("project") or selector.get("all")):
            raise ValueError("Choose project or session IDs before changing conversations")
        data = inventory()
        rows = selected_sessions(data, selector)
        if not rows:
            raise ValueError("No conversations match the selection")
        active = [row["id"] for row in rows if row["busy"]]
        if active:
            raise RuntimeError("Selected conversations are active or recently changed: " + ", ".join(active))
        proof = request.get("backup_proof") or {}
        covered = {(item["tool"], item["id"]) for item in proof.get("sessions", [])}
        if operation in ("archive", "delete"):
            if not result["backup_sha256"] or not all((row["tool"], row["id"]) in covered for row in rows):
                raise ValueError("A verified backup covering every selected conversation and child is required")
        backed_hashes = {}
        for item in proof.get("files", []):
            if item.get("owner_id"):
                backed_hashes.setdefault((item.get("tool"), item["owner_id"]), set()).add(item["sha256"])
        for row in rows:
            result["sessions"].append(row)
            for path in session_files(row):
                value = fingerprint(path)
                digest = file_hash(path)
                if fingerprint(path) != value:
                    raise RuntimeError("A session file changed while preparing the plan")
                if operation in ("archive", "delete") and digest not in backed_hashes.get((row["tool"], row["id"]), set()):
                    raise ValueError("The backup is missing a file or predates its current content: " + str(path))
                result["items"].append({"path": str(path), "tool": row["tool"], "owner_id": row["id"],
                                        "sha256": digest, **identity(path)})
    result["bytes"] = sum(item["size"] for item in result["items"])
    result["disk_bytes"] = sum(item["disk_bytes"] for item in result["items"])
    result["plan_id"] = plan_digest(result)
    return result


def apply_plan(request):
    plan = request["plan"]
    if plan.get("plan_id") != plan_digest(plan) or request.get("confirm") != plan["plan_id"]:
        raise ValueError("The exact plan ID is required and the plan must be unchanged")
    if plan["machine_id"] != machine_id():
        raise ValueError("This plan belongs to a different machine")
    if time.time() - plan["created_at"] > 86400:
        raise ValueError("This plan has expired; prepare a fresh preview")
    operation = plan["operation"]
    if operation not in ("archive", "unarchive", "delete", "cleanup"):
        raise ValueError("Unsupported maintenance operation")
    for item in plan["items"]:
        current = fingerprint(item["path"])
        if any(current[key] != item[key] for key in current):
            raise RuntimeError("A planned file changed; no operation was performed: " + item["path"])
        if item.get("sha256") and file_hash(item["path"]) != item["sha256"]:
            raise RuntimeError("A planned file no longer matches the verified backup")
    if operation == "cleanup":
        allowed = {item["path"] for item in cleanup_files(plan["categories"], plan["days"], plan.get("tool", "all"))}
        if any(item["path"] not in allowed for item in plan["items"]):
            raise RuntimeError("A cleanup file is outside the current eligible scope")
        for item in plan["items"]:
            Path(item["path"]).unlink()
        return {"operation": operation, "removed_files": len(plan["items"]), "bytes": plan["bytes"]}
    if operation in ("archive", "delete") and request.get("verified_backup_sha256") != plan.get("backup_sha256"):
        raise ValueError("Reverify the exact backup before applying this plan")
    current = inventory()
    by_id = {(row["tool"], row["id"]): row for row in current["sessions"]}
    for row in plan["sessions"]:
        actual = by_id.get((row["tool"], row["id"]))
        if not actual or actual["busy"] or actual["path"] != row["path"]:
            raise RuntimeError("A selected conversation is active, moved, or unavailable; prepare a fresh plan")
    targets = plan["sessions"]
    results = []
    for row in targets:
        if row["tool"] == "codex":
            if row.get("database"):
                with contextlib.closing(connect(row["database"])) as db:
                    state = db.execute("select archived,rollout_path from threads where id=?", (row["id"],)).fetchone()
                if operation == "delete" and (not state or not Path(state[1]).exists()):
                    results.append({"tool": "codex", "id": row["id"], "state": "applied"})
                    continue
                if operation in ("archive", "unarchive") and state and bool(state[0]) == (operation == "archive"):
                    results.append({"tool": "codex", "id": row["id"], "state": "applied"})
                    continue
            executable = shutil.which("codex")
            if not executable:
                raise RuntimeError("Codex CLI is unavailable")
            arguments = [executable, operation, row["id"]]
            if operation == "delete":
                arguments.append("--force")
            environment = dict(os.environ, CODEX_HOME=row["base"])
            response = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=120)
            if response.returncode:
                results.append({"tool": "codex", "id": row["id"], "state": "error",
                                "error": (response.stderr or response.stdout)[-2000:]})
                continue
        else:
            source = Path(row["base"])
            files = list(session_files(row))
            if operation == "delete":
                for path in files:
                    path.unlink()
            else:
                destination = roots()["claude-archive" if operation == "archive" else "claude"]
                pairs = [(path, destination / path.relative_to(source)) for path in files]
                if any(target.exists() for _, target in pairs):
                    raise RuntimeError("An archive destination already contains a selected file")
                for original, target in pairs:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.move(str(original), str(target))
        results.append({"tool": row["tool"], "id": row["id"], "state": "applied"})
    after = inventory()
    remaining = {(row["tool"], row["id"]): row for row in after["sessions"]}
    for result in results:
        if result["state"] != "applied":
            continue
        actual = remaining.get((result["tool"], result["id"]))
        valid = (not actual or not Path(actual["path"]).exists()) if operation == "delete" else actual and actual["archived"] == (operation == "archive")
        result["state"] = "verified" if valid else "needs_review"
    return {"operation": operation, "results": results,
            "all_verified": all(item["state"] == "verified" for item in results),
            "note": "Database files may retain free pages; archive changes organization without freeing transcript bytes."}


def version_key(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError("Invalid version: " + value)
    core = tuple(int(part) for part in match.group(1, 2, 3))
    prerelease = match.group(4)
    parts = tuple((0, int(item)) if item.isdigit() else (1, item) for item in prerelease.split(".")) if prerelease else ()
    return core + (int(prerelease is None), parts)


def fetch_text(url):
    limit = 2 << 20
    user_agent = "AgentMaintenance/" + VERSION
    try:
        with urlopen(Request(url, headers={"User-Agent": user_agent}), timeout=20) as response:
            body = response.read(limit + 1)
    except URLError as error:
        if not isinstance(error.reason, ssl.SSLCertVerificationError):
            raise
        # Framework Python can lack a CA bundle. The system curl retains certificate validation.
        curl = "/usr/bin/curl" if Path("/usr/bin/curl").is_file() else shutil.which("curl")
        if not curl:
            raise
        response = subprocess.run(
            [curl, "--fail", "--silent", "--show-error", "--location", "--max-time", "20",
             "--max-filesize", str(limit), "--proto", "=https", "--proto-redir", "=https",
             "--user-agent", user_agent, url],
            capture_output=True, timeout=25, check=True,
        )
        body = response.stdout
    if len(body) > limit:
        raise ValueError("The release channel response is too large")
    return body.decode().strip()


def latest_version(tool):
    if tool == "codex":
        errors = []
        for url in ("https://releases.openai.com/codex/channels/latest", "https://api.github.com/repos/openai/codex/releases/latest"):
            try:
                tag = json.loads(fetch_text(url))["tag_name"]
                if not isinstance(tag, str) or not tag.startswith("rust-v"):
                    raise ValueError("Unexpected release tag")
                version = tag[len("rust-v"):]
                version_key(version)
                return version
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(str(error))
        raise RuntimeError("Could not check the Codex release channel: " + "; ".join(errors))
    settings = roots()["claude"] / "settings.json"
    value = json.loads(settings.read_text()) if settings.exists() else {}
    channel = value.get("autoUpdatesChannel", "latest")
    if channel not in ("stable", "latest"):
        raise ValueError("Unsupported Claude update channel")
    version = fetch_text("https://downloads.claude.ai/claude-code-releases/" + channel)
    version_key(version)
    return version


def tool_versions(request):
    result = []
    for tool in ("codex", "claude") if request.get("tool", "all") == "all" else (request["tool"],):
        if tool not in ("codex", "claude"):
            raise ValueError("Unsupported CLI")
        item = {"tool": tool, "path": shutil.which(tool), "state": "missing"}
        result.append(item)
        if not item["path"]:
            continue
        try:
            response = subprocess.run([item["path"], "--version"], capture_output=True, text=True, timeout=20, check=True)
            match = re.search(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", response.stdout)
            if not match:
                raise ValueError("Could not parse the installed version")
            item.update(installed=match.group(0), state="installed")
            if request.get("check_latest") or request.get("apply"):
                item["latest"] = latest_version(tool)
                item["update_available"] = version_key(item["installed"]) < version_key(item["latest"])
            if request.get("apply") and item["update_available"]:
                response = subprocess.run([item["path"], "update"], stdin=subprocess.DEVNULL, capture_output=True,
                                          text=True, timeout=900, check=True)
                actual = subprocess.run([item["path"], "--version"], capture_output=True, text=True, timeout=20, check=True)
                match = re.search(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", actual.stdout)
                if not match or version_key(match.group(0)) < version_key(item["latest"]):
                    raise RuntimeError("The updated CLI did not pass version verification")
                item.update(installed=match.group(0), state="updated")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            item.update(state="error", error=str(error))
    return {"tools": result}


def dispatch(request, output=None):
    action = request.get("action", "inventory")
    if action == "inventory":
        return inventory(request.get("storage", False))
    if action == "diagnostics":
        return inventory()["diagnostics"]
    if action == "backup":
        backup(request["selector"], output or sys.stdout.buffer)
        return None
    if action == "plan":
        return make_plan(request)
    if action == "apply":
        return apply_plan(request)
    if action == "versions":
        return tool_versions(request)
    raise ValueError("Unknown maintenance action: " + str(action))


if __name__ == "__main__":
    request = json.loads(base64.b64decode(sys.argv[1])) if len(sys.argv) > 1 else {"action": "inventory"}
    try:
        if request.get("action") == "backup":
            sys.stdout.buffer.write(b"AGENT_MAINTENANCE_BACKUP\n")
            sys.stdout.buffer.flush()
            dispatch(request)
        else:
            print("AGENT_MAINTENANCE_JSON " + json.dumps({"ok": True, "result": dispatch(request)}, ensure_ascii=True))
    except Exception as error:
        if request.get("action") == "backup":
            print("Backup failed: " + str(error), file=sys.stderr)
        else:
            print("AGENT_MAINTENANCE_JSON " + json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
