#!/usr/bin/env python3
"""Inspect, back up, and maintain Codex and Claude conversations across SSH hosts."""
import argparse
import base64
import concurrent.futures
import contextlib
import datetime
import hashlib
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
import host as maintenance_engine

VERSION = "0.1.3"
ENGINE = Path(__file__).with_name("host.py")
SSH_OPTIONS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
               "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ControlMaster=no",
               "-o", "ControlPath=none", "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=2"]


def configuration_path():
    return Path(os.environ.get("AGENT_MAINTENANCE_CONFIG", str(Path.home() / ".config/agent-maintenance/config.json"))).expanduser()


def read_configuration():
    path = configuration_path()
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("The maintenance configuration must be a JSON object: " + str(path))
    return value


def ssh_hosts():
    """List literal aliases, including user configuration includes; never read identity files."""
    directory = Path.home() / ".ssh"
    seen, aliases = set(), []
    def visit(path, depth=0):
        if depth > 12 or path.resolve() in seen or not path.is_file():
            return
        seen.add(path.resolve())
        for line in path.read_text(errors="replace").splitlines():
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not parts:
                continue
            key, separator, inline = parts[0].partition("=")
            values = ([inline] if separator and inline else []) + parts[1:]
            if values and values[0] == "=":
                values = values[1:]
            if key.lower() == "host":
                aliases.extend(value for value in values
                               if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) and value != "local")
            elif key.lower() == "include":
                for value in values:
                    included = Path(value).expanduser()
                    if not included.is_absolute():
                        included = directory / included
                    for filename in sorted(glob.glob(str(included))):
                        visit(Path(filename), depth + 1)
    visit(directory / "config")
    return list(dict.fromkeys(aliases))


def host_configuration():
    override = os.environ.get("AGENT_MAINTENANCE_HOSTS")
    if override:
        values, source = list(filter(None, re.split(r"[,\s]+", override))), "environment"
    else:
        settings = read_configuration()
        if "hosts" in settings:
            values, source = settings["hosts"], "maintenance configuration"
        else:
            path = Path.home() / "Library/Application Support/AgentSpeed/SSHUsage/config.json"
            settings = json.loads(path.read_text()) if path.is_file() else {}
            if "hosts" in settings:
                values, source = settings["hosts"], "Agent Speed configuration"
            else:
                values, source = ssh_hosts(), "SSH configuration"
    values = [item["host"] if isinstance(item, dict) else item for item in values]
    if not all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in values):
        raise ValueError("Configured hosts must be literal SSH aliases")
    hosts = list(dict.fromkeys(["local", *values]))
    settings = read_configuration()
    profile = Path(__file__).with_name("config.example.json")
    defaults = json.loads(profile.read_text()) if profile.is_file() else {}
    preferred = settings.get("upgrade_hosts", defaults.get("upgrade_hosts", []))
    upgrade_hosts = [value for value in preferred if value in hosts and value != "local"]
    if not upgrade_hosts and "upgrade_hosts" not in settings:
        upgrade_hosts = [value for value in hosts if value != "local"]
    return {"hosts": hosts, "host_source": source, "upgrade_hosts": upgrade_hosts,
            "discovered_ssh_hosts": ssh_hosts(), "configuration_path": str(configuration_path())}


def configured_hosts():
    return host_configuration()["hosts"]


def configure(args):
    settings = read_configuration()
    changed = (args.hosts is not None or args.auto_hosts or args.codex_home is not None
               or args.claude_home is not None or args.upgrade_hosts is not None)
    if args.auto_hosts:
        settings.pop("hosts", None)
    elif args.hosts is not None:
        values = [value for value in re.split(r"[,\s]+", args.hosts) if value and value != "local"]
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in values):
            raise ValueError("Use literal SSH aliases separated by commas or newlines")
        settings["hosts"] = list(dict.fromkeys(values))
    if args.upgrade_hosts is not None:
        values = [value for value in re.split(r"[,\s]+", args.upgrade_hosts) if value and value != "local"]
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) for value in values):
            raise ValueError("Upgrade targets must be literal SSH aliases")
        settings["upgrade_hosts"] = list(dict.fromkeys(values))
    roots = settings.setdefault("data_roots", {})
    for key, value in (("codex", args.codex_home), ("claude", args.claude_home)):
        if value is not None:
            if value.strip():
                path = Path(value).expanduser().resolve()
                if path.exists() and not path.is_dir():
                    raise ValueError("An agent data home must be a directory: " + str(path))
                roots[key] = str(path)
            else:
                roots.pop(key, None)
    if changed:
        path = configuration_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            shutil.copy2(path, path.with_name("config.backup-" + str(time.time_ns()) + ".json"))
        temporary = path.with_name(".config-" + str(time.time_ns()) + ".tmp")
        try:
            temporary.write_text(json.dumps(settings, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**host_configuration(), "explicit_hosts": "hosts" in settings,
            "configured_data_roots": settings.get("data_roots", {}), "python": sys.executable,
            "local_diagnostics": invoke("local", {"action": "diagnostics"},
                                        progress=print_progress if args.progress else None)}


def size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000


def digest_file(path, progress=None):
    value = hashlib.sha256()
    count, last_report = 0, time.monotonic()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            value.update(block)
            count += len(block)
            if progress and time.monotonic() - last_report >= 1:
                progress("Archive checksum: " + size(count) + " read")
                last_report = time.monotonic()
    return value.hexdigest()


def command(host, request):
    payload = base64.b64encode(json.dumps(request, ensure_ascii=True).encode()).decode()
    environment = dict(os.environ)
    environment["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + environment.get("PATH", "")
    if host == "local":
        return [sys.executable, "-u", str(ENGINE), payload], None, environment
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("Invalid SSH host alias")
    script = 'export PATH="$HOME/.local/bin:$PATH"; exec python3 -u - ' + payload
    return ["/usr/bin/ssh", *SSH_OPTIONS, host, "bash -lc " + shlex.quote(script)], ENGINE.read_bytes(), environment


def print_progress(host, tool, message):
    message = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", message)
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
    label = host + ("/" + tool if tool else "")
    for line in message.replace("\r", "\n").splitlines():
        if line.strip():
            sys.stderr.write("[" + label + "] " + line + "\n")
            sys.stderr.flush()


class ProgressReader:
    def __init__(self, host, progress):
        self.host = host
        self.progress = progress
        self.pending = bytearray()

    def line(self, data):
        text = data.decode(errors="replace")
        prefix = "AGENT_MAINTENANCE_EVENT "
        if text.startswith(prefix):
            event = json.loads(text[len(prefix):])
            self.progress(self.host, event.get("tool", ""), event.get("message", ""))
        elif text.strip():
            self.progress(self.host, "", text)

    def __call__(self, channel, chunk):
        if channel != "stderr":
            return
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending = bytearray(rest)
            self.line(line)
        if len(self.pending) > (1 << 20):
            self.line(self.pending)
            self.pending.clear()

    def finish(self):
        if self.pending:
            self.line(self.pending)
            self.pending.clear()


def invoke(host, request, timeout=120, progress=None):
    if progress:
        request = {**request, "progress": True}
        progress(host, "", "Running " + request.get("action", "inventory"))
    arguments, source, environment = command(host, request)
    if progress:
        reader = ProgressReader(host, progress)
        result = maintenance_engine.stream_process(arguments, environment=environment, input_data=source,
                                                   timeout=timeout, output=reader)
        reader.finish()
    else:
        result = subprocess.run(arguments, input=source, env=environment, capture_output=True, timeout=timeout)
    prefix = "AGENT_MAINTENANCE_JSON "
    for line in result.stdout.decode(errors="replace").splitlines():
        if line.startswith(prefix):
            response = json.loads(line[len(prefix):])
            if response["ok"]:
                return response["result"]
            raise RuntimeError(response["error"])
    raise RuntimeError(result.stderr.decode(errors="replace").strip()[-1500:] or "No valid maintenance response")


def gather(hosts, request, timeout=120, progress=None):
    results, failures = {}, []
    workers = 1 if request.get("action") == "versions" and request.get("apply") else min(5, len(hosts))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        options = {"progress": progress} if progress else {}
        futures = {executor.submit(invoke, host, request, timeout, **options): host for host in hosts}
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            try:
                results[host] = future.result()
                for tool in results[host].get("tools", []):
                    if tool.get("state") == "error":
                        failures.append({"host": host, "tool": tool["tool"], "error": tool["error"]})
            except Exception as error:
                failures.append({"host": host, "error": str(error)})
                if progress:
                    progress(host, "", "Failed: " + str(error))
    return {"hosts": [{"host": host, **results[host]} for host in hosts if host in results], "failures": failures}


def selected(data, args, expand=False):
    import importlib.util
    spec = importlib.util.spec_from_file_location("maintenance_host", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.selected_sessions(data, selector(args), expand=expand)


def selector(args):
    return {"tool": getattr(args, "tool", "all"), "sessions": getattr(args, "session", None),
            "project": getattr(args, "project", None), "query": getattr(args, "query", ""),
            "archived": getattr(args, "archived", "all"),
            "include_subagents": getattr(args, "include_subagents", False), "all": getattr(args, "all", False)}


def safe_member(name):
    parts = Path(name).parts
    if name.startswith(("/", "\\")) or ".." in parts or "\\" in name or not parts:
        raise ValueError("Unsafe backup member: " + name)


def verify_backup(path, progress=None):
    path = Path(path).expanduser().resolve()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("The backup has duplicate members or no manifest")
        if archive.getinfo("manifest.json").file_size > (64 << 20):
            raise ValueError("The backup manifest is too large")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "agent-maintenance-backup" or manifest.get("format_version") != 1:
            raise ValueError("Unsupported backup format")
        records = {item["name"]: item for item in manifest["files"]}
        if len(records) != len(manifest["files"]) or set(records) | {"manifest.json"} != set(names):
            raise ValueError("The backup does not match its manifest")
        for name, item in records.items():
            if progress:
                progress("Verifying " + name)
            safe_member(name)
            info = archive.getinfo(name)
            if stat_mode(info) == 0o120000 or info.file_size != item["size"]:
                raise ValueError("A backup member has an invalid type or size")
            value = hashlib.sha256()
            count, last_report = 0, time.monotonic()
            with archive.open(name) as source:
                for block in iter(lambda: source.read(1 << 20), b""):
                    value.update(block)
                    count += len(block)
                    if progress and time.monotonic() - last_report >= 1:
                        progress(name + ": " + size(count) + "/" + size(item["size"]))
                        last_report = time.monotonic()
            if value.hexdigest() != item["sha256"]:
                raise ValueError("Backup checksum mismatch: " + name)
    if progress:
        progress("Verifying archive checksum")
    return {"path": str(path), "sha256": digest_file(path, progress), "sessions": len(manifest["sessions"]),
            "members": len(records), "bytes": path.stat().st_size, "manifest": manifest,
            "verified": True, "restore_mode": manifest.get("restore_mode", "standalone-files")}


def stat_mode(info):
    return (info.external_attr >> 16) & 0o170000


def stream_backup(host, selection, destination, progress=None):
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("The backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        raise ValueError("An incomplete backup already occupies the destination")
    request = {"action": "backup", "selector": selection, "progress": bool(progress)}
    arguments, source, environment = command(host, request)
    reader = ProgressReader(host, progress) if progress else None
    pending, found = bytearray(), False
    with open(destination.with_name(destination.name + ".log"), "xb") as error_log, open(temporary, "xb") as output:
        def receive(channel, chunk):
            nonlocal found
            if channel == "stderr":
                error_log.write(chunk)
                if reader:
                    reader(channel, chunk)
            elif found:
                output.write(chunk)
            else:
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    if line == b"AGENT_MAINTENANCE_BACKUP":
                        found = True
                        output.write(pending)
                        pending.clear()
                        break
                if len(pending) > 65536:
                    raise RuntimeError("Backup stream did not provide its header")
        result = maintenance_engine.stream_process(arguments, environment=environment, input_data=source,
                                                   timeout=86400, output=receive, stdout_limit=0)
        if reader:
            reader.finish()
    if not found:
        raise RuntimeError("Backup did not start; inspect " + str(destination.with_name(destination.name + ".log")))
    if result.returncode:
        raise RuntimeError("Backup failed; the partial file and log were retained for review")
    verify_progress = (lambda message: progress(host, "backup", message)) if progress else None
    proof = verify_backup(temporary, verify_progress)
    temporary.replace(destination)
    proof["path"] = str(destination)
    receipt = {key: value for key, value in proof.items() if key != "manifest"}
    receipt["host"] = host
    destination.with_suffix(".verified.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if progress:
        progress(host, "backup", "Backup verified: " + str(destination))
    return receipt


def restore_files(backup, destination, progress=None):
    proof = verify_backup(backup, progress)
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("Choose a new, empty restore directory")
    destination.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(backup) as archive:
        for item in archive.infolist():
            if progress:
                progress("Restoring " + item.filename)
            safe_member(item.filename)
            target = destination / item.filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(item) as source, open(target, "xb") as output:
                shutil.copyfileobj(source, output, 1 << 20)
            target.chmod(0o600)
    return {"destination": str(destination), "verified": True, "mode": "standalone-files",
            "note": "Original files, media, readable exports, and database row exports were restored. Native sidebar re-import is not performed."}


def common(parser, root=False):
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--host", action="append", default=default,
                        help="Host alias; repeat for multiple hosts, or use all configured hosts")
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS,
                        help="Print structured JSON for automation and desktop clients")
    parser.add_argument("--progress", action="store_true", default=False if root else argparse.SUPPRESS,
                        help="Stream operation progress to stderr while retaining structured stdout")


def selection_arguments(parser):
    parser.add_argument("--tool", choices=("all", "codex", "claude"), default="all")
    parser.add_argument("--session", action="append", help="Exact conversation UUID; repeat to select several")
    parser.add_argument("--project", help="Exact project name, project ID, or working directory")
    parser.add_argument("--query", default="", help="Filter by title, working directory, or UUID")
    parser.add_argument("--archived", choices=("all", "yes", "no"), default="all")
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument("--all", action="store_true", help="Explicitly select every matching conversation")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VERSION)
    common(root, True)
    commands = root.add_subparsers(dest="command", required=True)
    child = commands.add_parser("hosts", help="List local and configured SSH hosts")
    common(child)
    child = commands.add_parser("config", help="Read or update host and local data-source settings")
    common(child)
    group = child.add_mutually_exclusive_group()
    group.add_argument("--hosts", help="Comma or whitespace-separated aliases; empty means local only")
    group.add_argument("--auto-hosts", action="store_true", help="Restore automatic host discovery")
    child.add_argument("--codex-home", help="Local Codex data directory; empty restores automatic selection")
    child.add_argument("--claude-home", help="Local Claude data directory; empty restores automatic selection")
    child.add_argument("--upgrade-hosts", help="Preferred SSH servers for CLI checks and upgrades")
    child = commands.add_parser("diagnose", help="Report data roots and record counts without conversation content")
    common(child)
    child = commands.add_parser("interactive", help="Browse and maintain conversations interactively")
    common(child)
    selection_arguments(child)
    for name in ("inspect", "sessions", "projects"):
        child = commands.add_parser(name, help={"inspect": "Inspect storage categories and sessions", "sessions": "List active and archived conversations", "projects": "Group conversation sizes by project"}[name])
        common(child)
        selection_arguments(child)
    child = commands.add_parser("backup", help="Stream a verified compressed backup from a selected host")
    common(child)
    selection_arguments(child)
    child.add_argument("--output", type=Path, required=True, help="ZIP file for one host, or a directory for multiple hosts")
    child = commands.add_parser("verify", help="Verify every backup member and its SHA-256")
    common(child)
    child.add_argument("backup", type=Path)
    child = commands.add_parser("restore-files", help="Restore a verified backup into a standalone directory")
    common(child)
    child.add_argument("backup", type=Path)
    child.add_argument("--output", type=Path, required=True)
    child = commands.add_parser("plan", help="Preview conversation or temporary-file maintenance")
    common(child)
    selection_arguments(child)
    child.add_argument("operation", choices=("archive", "unarchive", "delete", "cleanup"))
    child.add_argument("--backup", type=Path, help="Optional legacy argument; archive and delete use manual backups")
    child.add_argument("--category", choices=("logs", "temp"), action="append")
    child.add_argument("--older-than", type=int, default=30, metavar="DAYS")
    child.add_argument("--output", type=Path, required=True)
    child = commands.add_parser("apply", help="Apply an unchanged reviewed plan with an exact confirmation ID")
    common(child)
    child.add_argument("plan", type=Path)
    child.add_argument("--confirm", required=True, help="Exact plan_id shown by the preview")
    child = commands.add_parser("upgrade", help="Check installed CLIs; --apply performs available updates")
    common(child)
    child.add_argument("--tool", choices=("all", "codex", "claude"), default="all")
    child.add_argument("--apply", action="store_true")
    return root


def host_names(args):
    if args.command == "upgrade" and not args.host:
        targets = host_configuration()["upgrade_hosts"]
        if not targets:
            raise ValueError("No SSH upgrade targets configured. Choose --host explicitly or configure remote servers.")
        return targets
    values = args.host or ["all"]
    return list(dict.fromkeys(host for value in values for host in (configured_hosts() if value == "all" else [value])))


def interactive(args):
    hosts = host_names(args)
    if len(hosts) != 1:
        print("Available hosts: " + ", ".join(hosts))
        target = input("Host [local]: ").strip() or "local"
        if target not in hosts:
            raise ValueError("Choose a configured host, or pass --host explicitly")
    else:
        target = hosts[0]
    while True:
        data = invoke(target, {"action": "inventory"})
        rows = selected(data, args)
        print("\n" + target + ":")
        for number, row in enumerate(rows, 1):
            print(f"{number:3}. {row['tool']:6} {'archived' if row['archived'] else 'active':8} "
                  f"{size(row['bytes']):>10} {row['title'][:70]} / {row['project']}")
        print("Commands: inspect, projects, backup, archive, unarchive, delete, cleanup, upgrade, refresh, quit")
        operation = input("Action [refresh]: ").strip().lower() or "refresh"
        if operation in ("q", "quit", "exit"):
            return 0
        if operation == "refresh":
            continue
        base = ["--host", target]
        if operation in ("inspect", "projects", "upgrade"):
            main([operation, *base])
            continue
        if operation not in ("backup", "archive", "unarchive", "delete", "cleanup"):
            print("Unknown action")
            continue
        scope = []
        if operation != "cleanup":
            choice = input("Conversation numbers (comma separated), or project:<exact project>: ").strip()
            if choice.startswith("project:"):
                scope = ["--project", choice.split(":", 1)[1]]
            else:
                indices = [int(value) - 1 for value in choice.split(",") if value.strip()]
                if not indices or any(index < 0 or index >= len(rows) for index in indices):
                    raise ValueError("Choose valid conversation numbers")
                scope = [value for index in indices for value in ("--session", rows[index]["id"])]
        if operation == "backup":
            destination = input("New backup ZIP path: ").strip()
            if destination:
                main(["backup", *base, *scope, "--output", str(Path(destination).expanduser())])
            continue
        directory = Path.home() / ".local/state/agent-maintenance/plans"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        plan_path = directory / (str(__import__("time").time_ns()) + ".json")
        if main(["plan", operation, *base, *scope, "--output", str(plan_path)]) != 0:
            continue
        reviewed = json.loads(plan_path.read_text())["plan"]
        confirmation = input("Type the full plan ID to apply, or press Enter to cancel: ").strip()
        if confirmation == reviewed["plan_id"]:
            main(["apply", str(plan_path), "--confirm", confirmation])
        else:
            print("Cancelled")


def output_result(value, args):
    if args.json:
        print(json.dumps(value, ensure_ascii=True, indent=2))
        return
    if args.command in ("inspect", "sessions", "projects"):
        if args.command == "inspect":
            for host in value["hosts"]:
                children = sum(row.get('is_subagent', False) for row in host['sessions'])
                print(f"\n{host['host']}: {len(host['sessions']) - children} conversations; {children} subagent records; {sum(row['busy'] for row in host['sessions'])} active or recently updated")
                for item in sorted(host["storage"], key=lambda row: row["bytes"], reverse=True):
                    print(f"  {item['tool']:15} {item['name']:28} {size(item['bytes']):>12}  {item['files']:>7} files")
        elif args.command == "sessions":
            print(f"{'Host':12} {'Tool':7} {'State':9} {'Size':>11} {'UUID':36} Title / project")
            for host in value["hosts"]:
                for row in host["sessions"]:
                    print(f"{host['host']:12} {row['tool']:7} {'archived' if row['archived'] else 'active':9} {size(row['bytes']):>11} {row['id']} {row['title'][:65]} / {row['project']}")
        else:
            print(f"{'Host':12} {'Project':30} {'Chats':>7} {'Transcript size':>17}")
            for row in value["projects"]:
                print(f"{row['host']:12} {row['project'][:30]:30} {row['sessions']:>7} {size(row['bytes']):>17}")
        for failure in value.get("failures", []):
            print(f"{failure['host']}: {failure['error']}", file=sys.stderr)
    elif args.command == "plan":
        plan = value["plan"]
        print(f"Preview: {plan['operation']} on {value['host']}")
        print(f"Conversations: {len(plan['sessions'])}; files: {len(plan['items'])}; size: {size(plan['bytes'])}")
        for row in plan["sessions"]:
            print(f"  {row['tool']} {row['id']}  {row['title'][:90]}")
        print("Archive preserves transcript size. Backups are managed separately.")
        print("Plan saved: " + str(args.output.resolve()))
        print("Apply: agent-maintenance apply " + shlex.quote(str(args.output.resolve())) + " --confirm " + plan["plan_id"])
    else:
        print(json.dumps(value, ensure_ascii=True, indent=2))


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        progress = print_progress if args.progress else None
        hosts = host_names(args) if args.command not in ("config", "hosts", "verify", "restore-files", "apply") else ["local"]
        if args.command == "hosts":
            result = host_configuration()
        elif args.command == "config":
            result = configure(args)
        elif args.command == "diagnose":
            result = gather(hosts, {"action": "diagnostics"}, progress=progress)
        elif args.command == "interactive":
            return interactive(args)
        elif args.command in ("inspect", "sessions", "projects"):
            result = gather(hosts, {"action": "inventory", "storage": args.command == "inspect"}, progress=progress)
            if args.command != "inspect":
                for host in result["hosts"]:
                    host["sessions"] = selected(host, args, expand=args.command == "projects")
            if args.command == "projects":
                groups = {}
                for host in result["hosts"]:
                    for row in host["sessions"]:
                        key = (host["host"], row.get("project_id") or row.get("project_path") or row["project"])
                        group = groups.setdefault(key, {"host": host["host"], "project": row["project"],
                                                       "project_id": row.get("project_id"), "project_path": row.get("project_path"),
                                                       "sessions": 0, "subagents": 0, "bytes": 0})
                        group["subagents" if row.get("is_subagent") else "sessions"] += 1
                        group["bytes"] += row["bytes"]
                result["projects"] = sorted(groups.values(), key=lambda row: row["bytes"], reverse=True)
        elif args.command == "backup":
            result = {"backups": [], "failures": []}
            if not (args.session or args.project or args.all):
                raise ValueError("Choose --project, --session, or --all")
            for host in hosts:
                try:
                    data = invoke(host, {"action": "inventory"}, progress=progress)
                    if not selected(data, args, expand=True):
                        continue
                    destination = args.output
                    if len(hosts) > 1:
                        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        destination = destination / (stamp + "-" + host + ".zip")
                    print("Backing up " + host + " to " + str(destination), file=sys.stderr, flush=True)
                    result["backups"].append(stream_backup(host, selector(args), destination, progress))
                except Exception as error:
                    result["failures"].append({"host": host, "error": str(error)})
            if not result["backups"] and not result["failures"]:
                raise ValueError("No conversations match the selection")
        elif args.command == "verify":
            proof = verify_backup(args.backup, (lambda text: progress("local", "backup", text)) if progress else None)
            result = {key: value for key, value in proof.items() if key != "manifest"}
        elif args.command == "restore-files":
            result = restore_files(args.backup, args.output, (lambda text: progress("local", "restore", text)) if progress else None)
        elif args.command == "plan":
            if len(hosts) != 1:
                raise ValueError("Select one --host for each reviewed maintenance plan")
            request = {"action": "plan", "operation": args.operation, "selector": selector(args),
                       "categories": args.category, "days": args.older_than}
            plan = invoke(hosts[0], request, timeout=3600, progress=progress)
            result = {"format": "agent-maintenance-client-plan", "host": hosts[0], "plan": plan}
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.output.open("x") as handle:
                json.dump(result, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
        elif args.command == "apply":
            wrapper = json.loads(args.plan.read_text())
            if wrapper.get("format") != "agent-maintenance-client-plan":
                raise ValueError("Unsupported maintenance plan")
            plan = wrapper["plan"]
            request = {"action": "apply", "plan": plan, "confirm": args.confirm}
            result = invoke(wrapper["host"], request, timeout=3600, progress=progress)
        else:
            result = gather(hosts, {"action": "versions", "tool": args.tool, "check_latest": True, "apply": args.apply},
                            timeout=2100 if args.apply else 90, progress=progress)
        output_result(result, args)
        return 1 if result.get("failures") or result.get("all_verified") is False else 0
    except Exception as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
