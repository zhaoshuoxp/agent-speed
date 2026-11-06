#!/usr/bin/env python3
"""Mirror SSH transcript files for Agent Speed; never modify remote stores."""
import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import plistlib
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

LABEL = "io.github.zoefix.agentspeed.ssh-usage"
VERSION = 2

SSH_OPTIONS = [
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
    "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ControlMaster=no",
    "-o", "ControlPath=none", "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=2",
]


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def is_transcript(path, source):
    return path.endswith(".jsonl") or source == "codex" and path.endswith(".jsonl.zst")


def validate_config(config, root):
    root = root.resolve()
    data = root / "data"
    if pathlib.Path(config["state"]).resolve() != root or pathlib.Path(config["data"]).resolve() != data:
        raise ValueError("The SSH cache must be inside its application data directory")
    for store in config.get("local", []):
        parts = pathlib.PurePosixPath(store["destination"])
        if (store["source"] not in ("codex", "claude") or parts.is_absolute() or ".." in parts.parts
                or len(parts.parts) < 3 or parts.parts[:2] != (store["source"], "local")):
            raise ValueError("A local mirror path is outside its cache directory")
        if data.joinpath(*parts.parts).resolve() != data.joinpath(*parts.parts):
            raise ValueError("Symlinked cache directories cannot be managed")
    hosts = set()
    for host in config["hosts"]:
        name = host["host"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name == "local" or name in hosts:
            raise ValueError("Invalid or duplicate SSH host alias")
        hosts.add(name)
        for store in host["stores"]:
            parts = pathlib.PurePosixPath(store["destination"])
            if (store["source"] not in ("codex", "claude") or parts.is_absolute()
                    or ".." in parts.parts or len(parts.parts) < 3
                    or parts.parts[:2] != (store["source"], name)):
                raise ValueError("A remote cache path is outside its host directory")
            path = data.joinpath(*parts.parts)
            if path.resolve() != path:
                raise ValueError("Symlinked cache directories cannot be managed")


def cached_files(config, host_name=None):
    data = pathlib.Path(config["data"])
    for host in config["hosts"]:
        if host_name and host["host"] != host_name:
            continue
        for source in ("codex", "claude"):
            folder = data / source / host["host"]
            if folder.is_symlink():
                continue
            for directory, names, files in os.walk(folder, followlinks=False):
                names[:] = [name for name in names if not os.path.islink(os.path.join(directory, name))]
                for name in files:
                    if not is_transcript(name, source):
                        continue
                    path = pathlib.Path(directory) / name
                    try:
                        info = path.lstat()
                        if stat.S_ISREG(info.st_mode):
                            yield host["host"], source, path, info
                    except FileNotFoundError:
                        continue


@contextlib.contextmanager
def state_lock(root, blocking=False):
    with (root / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise RuntimeError("A sync is running. Wait for it to finish or turn background sync off, then retry.") from error
        yield


def same_version(left, right):
    return bool(left and right and left.get("size") == right.get("size")
                and left.get("modified_ns") == right.get("modified_ns"))


def select_files(store, rows, excluded):
    selected, versions, skipped = [], {}, 0
    for relative, size, modified_ns in rows:
        path = pathlib.PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not is_transcript(relative, store["source"]):
            raise ValueError("The server returned an invalid transcript path")
        key = store["destination"] + "/" + relative
        version = {"size": size, "modified_ns": modified_ns}
        versions[key] = version
        if same_version(excluded.get(key), version):
            skipped += 1
        else:
            selected.append(relative)
    return selected, versions, skipped


def remote_inventory(host, config):
    roots = [(store["path"], store["source"]) for store in host["stores"]]
    code = """import json,os,stat
roots = ROOTS
result = []
for root,source in roots:
    rows = []
    exists = os.path.isdir(root)
    if exists:
        for directory,names,files in os.walk(root,followlinks=False):
            names[:] = [n for n in names if not os.path.islink(os.path.join(directory,n))]
            for name in files:
                if not (name.endswith('.jsonl') or source=='codex' and name.endswith('.jsonl.zst')):
                    continue
                path = os.path.join(directory,name)
                try:
                    info = os.lstat(path)
                    if stat.S_ISREG(info.st_mode):
                        rows.append([os.path.relpath(path,root),info.st_size,info.st_mtime_ns])
                except FileNotFoundError:
                    pass
    result.append({'available':exists,'files':rows})
print('AGENT_SPEED_INVENTORY '+json.dumps(result))
""".replace("ROOTS", repr(roots), 1)
    response = subprocess.run(
        [config["ssh"], *SSH_OPTIONS, host["host"], "python3 -"], input=code.encode(),
        capture_output=True, timeout=45
    )
    if response.returncode:
        raise RuntimeError(response.stderr.decode(errors="replace").strip()[-1200:] or "SSH inventory failed")
    prefix = "AGENT_SPEED_INVENTORY "
    for line in response.stdout.decode(errors="replace").splitlines():
        if line.startswith(prefix):
            result = json.loads(line[len(prefix):])
            if len(result) == len(roots):
                return result
    raise RuntimeError("Unexpected SSH transcript inventory")


def sync_local(config):
    linked = copied = files = stale_links_removed = 0
    indexed = None
    for store in config["local"]:
        source = pathlib.Path(store["path"])
        destination = pathlib.Path(config["data"]) / store["destination"]
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not source.is_dir():
            continue
        for directory, names, filenames in os.walk(source, followlinks=False):
            names[:] = [name for name in names if not os.path.islink(os.path.join(directory, name))]
            for name in filenames:
                path = pathlib.Path(directory) / name
                if not is_transcript(name, store["source"]) or path.is_symlink():
                    continue
                target = destination / path.relative_to(source)
                try:
                    info = path.stat()
                    files += 1
                    if target.exists() and os.path.samefile(path, target):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = target.with_name("." + target.name + ".linking")
                    temporary.unlink(missing_ok=True)
                    try:
                        os.link(path, temporary)
                        linked += 1
                    except OSError:
                        if target.exists():
                            old = target.stat()
                            if old.st_size == info.st_size and old.st_mtime_ns == info.st_mtime_ns:
                                continue
                        shutil.copy2(path, temporary)
                        temporary.chmod(0o600)
                        copied += 1
                    temporary.replace(target)
                except FileNotFoundError:
                    continue
        for directory, names, filenames in os.walk(destination, followlinks=False):
            names[:] = [name for name in names if not os.path.islink(os.path.join(directory, name))]
            for name in filenames:
                target = pathlib.Path(directory) / name
                if not is_transcript(name, store["source"]) or target.is_symlink():
                    continue
                original = source / target.relative_to(destination)
                if original.exists():
                    continue
                try:
                    if indexed is None:
                        history = pathlib.Path(config["state"]).parent / "history.sqlite"
                        if not history.exists():
                            indexed = {}
                        else:
                            with contextlib.closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True)) as db:
                                indexed = {row[0]: (row[1], row[2]) for row in db.execute("select path,size,modified from files")}
                    info = target.stat()
                    if indexed.get(str(target)) == (info.st_size, info.st_mtime_ns):
                        target.unlink()
                        stale_links_removed += 1
                except (FileNotFoundError, sqlite3.Error):
                    continue
    return {"state": "ok", "files": files, "new_hardlinks": linked, "new_copies": copied,
            "stale_links_removed": stale_links_removed}


def sync_host(host, config, previous, excluded):
    name = host["host"]
    result = {"host": name, "state": "error", "checked_at": timestamp(),
              "last_success": previous.get("last_success"), "stores": []}
    versions = {}
    try:
        inventory = remote_inventory(host, config)
        warnings = []
        for store, remote in zip(host["stores"], inventory):
            exists = remote["available"]
            item = {"source": store["source"], "kind": store["kind"], "path": store["path"],
                    "destination": store["destination"], "available": exists}
            result["stores"].append(item)
            destination = pathlib.Path(config["data"]) / store["destination"]
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not exists:
                item["state"] = "no_logs"
                continue
            selected, current_versions, skipped = select_files(store, remote["files"], excluded)
            versions.update(current_versions)
            item["cleaned_files_skipped"] = skipped
            if not selected:
                item.update(state="ok", returncode=0, transferred_files=0, transferred_bytes=0)
                continue
            command = [
                config["rsync"], "-rtz", "--protect-args", "--omit-dir-times", "--timeout=90",
                "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=", "--include=*/", "--include=*.jsonl",
                "--from0", "--files-from=-",
            ]
            if store["source"] == "codex":
                command += ["--include=*.jsonl.zst"]
            command += ["--exclude=*", "--stats", "-e", shlex.join([config["ssh"], *SSH_OPTIONS]),
                        name + ":" + store["path"].rstrip("/") + "/", str(destination) + "/"]
            file_list = b"\0".join(os.fsencode(path) for path in selected) + b"\0"
            transfer = subprocess.run(command, input=file_list, capture_output=True, timeout=900)
            item["returncode"] = transfer.returncode
            item["state"] = "ok" if transfer.returncode == 0 else "retry" if transfer.returncode == 24 else "error"
            for line in transfer.stdout.decode(errors="replace").splitlines():
                if line.startswith("Number of regular files transferred:"):
                    item["transferred_files"] = int(line.rsplit(":", 1)[1].strip().replace(",", ""))
                elif line.startswith("Total transferred file size:"):
                    item["transferred_bytes"] = int(line.rsplit(":", 1)[1].strip().split()[0].replace(",", ""))
            if transfer.returncode not in (0, 24):
                raise RuntimeError(transfer.stderr.decode(errors="replace").strip()[-1200:] or "rsync failed")
            if transfer.returncode == 24:
                warnings.append("A source file moved during transfer; it will be retried next cycle")
        result.update(state="ok", last_success=timestamp())
        if warnings:
            result["warnings"] = warnings
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result, versions


def run(config_path, force=False, quiet=False):
    os.umask(0o077)
    config = json.loads(config_path.read_text())
    state_directory = pathlib.Path(config["state"])
    validate_config(config, config_path.parent)
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not config.get("enabled", True) and not force:
        return 0
    with (state_directory / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        started = time.monotonic()
        status_path = state_directory / "status.json"
        previous = read_json(status_path, {})
        excluded = read_json(state_directory / "cleaned-files.json", {})
        remote_versions = read_json(state_directory / "remote-index.json", {})
        old_hosts = {host["host"]: host for host in previous.get("hosts", [])}
        status = {"started_at": timestamp(), "state": "syncing", "interval_seconds": 30,
                  "network_traffic_collection": False, "hosts": [],
                  "last_success": previous.get("completed_at", previous.get("last_success"))}
        atomic_json(status_path, status)
        status["local"] = sync_local(config)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(sync_host, host, config, old_hosts.get(host["host"], {}), excluded): host["host"]
                for host in config["hosts"]
            }
            for future in concurrent.futures.as_completed(futures):
                result, versions = future.result()
                remote_versions.update(versions)
                atomic_json(state_directory / "remote-index.json", remote_versions)
                status["hosts"].append(result)
                atomic_json(status_path, status)
                if not quiet and (config.get("verbose") or result["state"] != "ok"):
                    print(json.dumps({"host": result["host"], "state": result["state"],
                                      "error": result.get("error")}), flush=True)
        status["hosts"].sort(key=lambda host: [item["host"] for item in config["hosts"]].index(host["host"]))
        status.update(state="ok" if all(host["state"] == "ok" for host in status["hosts"]) else "partial",
                      completed_at=timestamp(), elapsed_seconds=round(time.monotonic() - started, 2))
        atomic_json(status_path, status)
        return 0 if status["state"] == "ok" else 1


def service_loaded():
    return subprocess.run(
        ["/bin/launchctl", "print", "gui/" + str(os.getuid()) + "/" + LABEL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode == 0


def cache_info(config_path):
    if not config_path.exists():
        return {"configured": False, "enabled": False, "service_loaded": False,
                "state": "unconfigured", "interval_seconds": 30, "files": 0, "bytes": 0,
                "disk_bytes": 0, "hosts": []}
    config = json.loads(config_path.read_text())
    validate_config(config, config_path.parent)
    status = read_json(config_path.parent / "status.json", {})
    states = {item["host"]: item for item in status.get("hosts", [])}
    result = {"configured": True, "enabled": config.get("enabled", True), "service_loaded": service_loaded(),
              "state": status.get("state", "idle") if config.get("enabled", True) else "paused",
              "last_sync": status.get("completed_at", status.get("last_success")),
              "interval_seconds": 30, "files": 0, "bytes": 0, "disk_bytes": 0, "hosts": []}
    hosts = {}
    for configured in config["hosts"]:
        name = configured["host"]
        state = states.get(name, {})
        item = {"host": name, "files": 0, "bytes": 0, "disk_bytes": 0,
                "codex_files": 0, "codex_bytes": 0, "claude_files": 0, "claude_bytes": 0,
                "state": state.get("state", "idle"), "last_success": state.get("last_success"),
                "error": state.get("error")}
        hosts[name] = item
        result["hosts"].append(item)
    for host, source, path, info in cached_files(config):
        item = hosts[host]
        for target in (item, result):
            target["files"] += 1
            target["bytes"] += info.st_size
            target["disk_bytes"] += info.st_blocks * 512
        item[source + "_files"] += 1
        item[source + "_bytes"] += info.st_size
    return result


def clear_cache(config_path, host_name=None, days=30, dry_run=False):
    config = json.loads(config_path.read_text())
    root = config_path.parent
    validate_config(config, root)
    if host_name and host_name not in {host["host"] for host in config["hosts"]}:
        raise ValueError("Unknown SSH host")
    if days not in (0, 7, 30, 90):
        raise ValueError("Unsupported cleanup age")
    history = root.parent / "history.sqlite"
    if not history.exists():
        raise RuntimeError("Open Usage History and refresh it before clearing downloaded logs")
    with state_lock(root):
        with contextlib.closing(sqlite3.connect(history.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            indexed = {row[0]: (row[1], row[2]) for row in connection.execute("select path,size,modified from files")}
        remote_versions = read_json(root / "remote-index.json", {})
        excluded = read_json(root / "cleaned-files.json", {})
        cutoff = time.time() - days * 86400 if days else None
        candidates, pending = [], 0
        for host, source, path, info in cached_files(config, host_name):
            if cutoff is not None and info.st_mtime >= cutoff:
                continue
            key = path.relative_to(config["data"]).as_posix()
            remote = remote_versions.get(key)
            if (indexed.get(str(path)) != (info.st_size, info.st_mtime_ns) or not remote
                    or remote.get("size") != info.st_size
                    or remote.get("modified_ns", -1) // 1_000_000_000 != info.st_mtime_ns // 1_000_000_000):
                pending += 1
                continue
            candidates.append((path, info, key, remote))
        removed_bytes = sum(info.st_size for path, info, key, remote in candidates)
        disk_bytes = sum(info.st_blocks * 512 for path, info, key, remote in candidates)
        if not dry_run:
            for path, info, key, remote in candidates:
                excluded[key] = {**remote, "cleaned_at": timestamp()}
            atomic_json(root / "cleaned-files.json", excluded)
            for path, info, key, remote in candidates:
                path.unlink()
        return {"removed_files": len(candidates), "removed_bytes": removed_bytes,
                "freed_disk_bytes": disk_bytes, "pending_files": pending, "dry_run": dry_run}


def set_service(config_path, enabled):
    root = config_path.parent
    config = json.loads(config_path.read_text())
    validate_config(config, root)
    service = "gui/" + str(os.getuid()) + "/" + LABEL
    config["enabled"] = enabled
    atomic_json(config_path, config)
    agent = pathlib.Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    agent.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        body = {"Label": LABEL, "ProgramArguments": [sys.executable, str(root / "sync_usage.py")],
                "RunAtLoad": True, "StartInterval": 30, "ProcessType": "Background",
                "LowPriorityIO": True, "Nice": 10,
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                "StandardOutPath": str(root / "sync.log"), "StandardErrorPath": str(root / "sync-error.log")}
        temporary = agent.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(body))
        temporary.chmod(0o600)
        temporary.replace(agent)
        subprocess.run(["/bin/launchctl", "enable", service], check=True, capture_output=True)
        if not service_loaded():
            subprocess.run(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(agent)],
                           check=True, capture_output=True)
    else:
        subprocess.run(["/bin/launchctl", "disable", service], check=True, capture_output=True)
        if service_loaded():
            subprocess.run(["/bin/launchctl", "bootout", service], check=True, capture_output=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(__file__).with_name("config.json"))
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--info", action="store_true")
    commands.add_argument("--sync", action="store_true")
    commands.add_argument("--service", choices=("enable", "disable"))
    commands.add_argument("--clear", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not (args.info or args.sync or args.service or args.clear):
        return run(args.config)
    try:
        message = None
        cleanup = None
        if args.service:
            set_service(args.config, args.service == "enable")
        elif args.sync:
            run(args.config, force=True, quiet=True)
        elif args.clear:
            cleanup = clear_cache(args.config, args.host, args.days, args.dry_run)
            verb = "Eligible for cleanup:" if args.dry_run else "Cleared"
            message = f"{verb} {cleanup['removed_files']} log files. Daily usage history is retained."
            if cleanup["pending_files"]:
                message += f" Kept {cleanup['pending_files']} files pending history indexing or sync."
        response = cache_info(args.config)
        if message:
            response["message"] = message
        if cleanup is not None:
            response["cleanup"] = cleanup
        print(json.dumps(response))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
