#!/usr/bin/env python3
"""Package the signed app, matching CLI, and installer without user configuration or data."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("app_zip", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parent
with zipfile.ZipFile(args.app_zip) as archive:
    if archive.testzip():
        raise SystemExit("The signed app archive is corrupt")
    info = plistlib.loads(archive.read("Agent Maintenance.app/Contents/Info.plist"))
    for name in ("host.py", "agent_maintenance.py", "find-python.sh", "README.txt"):
        if archive.read("Agent Maintenance.app/Contents/Resources/CLI/" + name) != (source / name).read_bytes():
            raise SystemExit("Rebuild the app before packaging: its bundled CLI differs from " + name)
version = info["CFBundleShortVersionString"]
name = "AgentMaintenance-Test-" + version + "-macOS"
output = args.output.resolve()
folder = output / name
package = output / (name + ".zip")
if folder.exists() or package.exists():
    raise SystemExit("Choose an output directory without this distribution version")
folder.mkdir(parents=True)
cli = folder / "cli"
cli.mkdir()
files = ["agent_maintenance.py", "host.py", "agent-maintenance", "find-python.sh", "install.py",
         "install_legacy.py", "legacy_codex_archives.py", "legacy_claude_sessions.py",
         "legacy_codex_update_server.sh", "README.txt", "test_maintenance.py", "validate_native_codex.py",
         "desktop/AgentMaintenance.swift", "desktop/build-app.sh", "desktop/verify-app.py",
         "desktop/assets/AgentMaintenance.svg", "desktop/assets/AgentMaintenance.icns",
         "desktop/assets/GenerateIcon.swift", "desktop/assets/icon-1024.png"]
for relative in files:
    destination = cli / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / relative, destination)
shutil.copy2(args.app_zip, folder / "AgentMaintenance-macOS.zip")
shutil.copy2(source / "Install.command", folder / "Install.command")
shutil.copy2(source / "DISTRIBUTION.txt", folder / "README.txt")
shutil.copy2(source / "config.example.json", folder / "config.example.json")
commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True)
metadata = {"product": "Agent Maintenance", "version": version, "build": info["CFBundleVersion"],
            "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_repository": "https://github.com/zhaoshuoxp/agent-speed",
            "source_commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "minimum_macos": info["LSMinimumSystemVersion"], "python_minimum": "3.9", "python_bundled": False,
            "architectures": ["arm64", "x86_64"], "desktop_visual_acceptance": "pending",
            "contents": "Signed app, matching CLI source, installer, and example aliases; no user data or credentials"}
(folder / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n")
manifest = []
for path in sorted(folder.rglob("*")):
    if path.is_file():
        manifest.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(folder).as_posix())
(folder / "SHA256SUMS.txt").write_text("\n".join(manifest) + "\n")
with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            archive.write(path, name + "/" + path.relative_to(folder).as_posix())
with zipfile.ZipFile(package) as archive:
    assert archive.testzip() is None
    for record in archive.read(name + "/SHA256SUMS.txt").decode().splitlines():
        digest, relative = record.split("  ", 1)
        assert hashlib.sha256(archive.read(name + "/" + relative)).hexdigest() == digest
package.with_suffix(".sha256").write_text(hashlib.sha256(package.read_bytes()).hexdigest() + "  " + package.name + "\n")
print(package)
